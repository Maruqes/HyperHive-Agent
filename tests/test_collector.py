import unittest
from unittest.mock import Mock, patch
import collector

class CollectorTests(unittest.TestCase):
    def test_cpu_and_counter_reset(self):
        self.assertEqual(collector.cpu_percent((100,40),(200,70)),70)
        self.assertIsNone(collector.cpu_percent((100,40),(90,41)))
        self.assertIsNone(collector.rate(20,10,1))
        self.assertEqual(collector.rate(10,20,2),5)

    def test_journal_routes_and_timestamp(self):
        entry = {'PRIORITY':'3','MESSAGE':'failed','_SYSTEMD_UNIT':'nginx.service','_PID':'8','__REALTIME_TIMESTAMP':'1000000'}
        result = collector.journal_samples(entry,['nginx.service'])
        self.assertEqual([x['stream'] for x in result],['journal_logs','system_logs','service_logs'])
        self.assertEqual(result[0]['value'],{'message':'nginx.service[8]: failed','severity':'error'})
        self.assertEqual(result[0]['timestamp'],'1970-01-01T00:00:01Z')

    def test_service_discovery_defaults_to_all_loaded_services(self):
        listing = 'a.service loaded active running A\nb.service loaded inactive dead B\n'
        def fake_command(args):
            if args[1] == 'list-units':
                self.assertNotIn('--failed', args)
                self.assertIn('--type=service', args)
                return listing
            name = args[2]
            return f'Id={name}\nLoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n'
        with patch.object(collector, 'command', side_effect=fake_command), patch.object(collector.subprocess,'run',side_effect=lambda args,**kw: Mock(returncode=0,stdout=f'Id={args[-1]}\nLoadState=loaded\nActiveState=active\n',stderr='')):
            self.assertEqual([s['unit'] for s in collector.system_services([])], ['a.service', 'b.service'])

    def test_configured_services_include_failed_units(self):
        def fake_command(args):
            if args[1] == 'list-units':
                self.assertIn('--failed', args)
                return 'other.service loaded failed failed Other\n'
            return f'Id={args[2]}\nLoadState=loaded\nActiveState=active\n'
        with patch.object(collector, 'command', side_effect=fake_command), patch.object(collector.subprocess,'run',side_effect=lambda args,**kw: Mock(returncode=0,stdout=f'Id={args[-1]}\nLoadState=loaded\nActiveState=active\n',stderr='')):
            self.assertEqual([s['unit'] for s in collector.system_services(['a.service'])], ['a.service', 'other.service'])

    def test_default_routes_all_service_logs(self):
        entry = {'_SYSTEMD_UNIT':'a.service','MESSAGE':'hello'}
        self.assertIn('service_logs', [s['stream'] for s in collector.journal_samples(entry, [])])
        self.assertNotIn('service_logs', [s['stream'] for s in collector.journal_samples(entry, ['b.service'])])
        self.assertNotIn('service_logs', [s['stream'] for s in collector.journal_samples({'MESSAGE':'hello'}, [])])

    def test_default_interfaces_include_all_and_configured_limits_selection(self):
        from pathlib import Path
        with patch.object(collector, 'Path') as paths:
            paths.return_value.iterdir.return_value = [Path('/sys/class/net/eth0'), Path('/sys/class/net/lo'), Path('/sys/class/net/veth0')]
            self.assertEqual(collector.interfaces([]), ['eth0', 'lo', 'veth0'])
            self.assertEqual(collector.interfaces(['eth0']), ['eth0'])

    def test_default_uplinks_exclude_virtual_and_loopback(self):
        routes = {'/proc/net/route': 'header\neth0 00000000 00000000 0001\nveth0 00000000 00000000 0001\n', '/proc/net/ipv6_route': ''}
        with patch.object(collector, 'read', side_effect=lambda path: routes[path]):
            self.assertEqual(collector.uplinks(), ['eth0'])

    def test_tls_config_defaults_to_verified_and_accepts_opt_out(self):
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w') as f:
            f.write('SUPPORT_INGEST_URL=http://example/ingest\nSUPPORT_APP_ID=a\nSUPPORT_APP_SECRET=b\n'); f.flush()
            self.assertTrue(collector.load_env(f.name)['SUPPORT_TLS_VERIFY'])
            f.write('SUPPORT_TLS_VERIFY=false\n'); f.flush()
            self.assertFalse(collector.load_env(f.name)['SUPPORT_TLS_VERIFY'])
            f.write('SUPPORT_TLS_VERIFY=invalid\n'); f.flush()
            with self.assertRaises(ValueError): collector.load_env(f.name)

    def test_process_pid_reuse_and_deduplication(self):
        old = {1:{'ticks':10,'start':1},2:{'ticks':10,'start':1}}
        new = {1:{'pid':1,'name':'a','ticks':20,'start':1,'resident_memory_bytes':100},2:{'pid':2,'name':'b','ticks':20,'start':2,'resident_memory_bytes':200}}
        result = collector.top_processes(old,new,1)
        self.assertEqual([p['pid'] for p in result],[1])

    def test_event_fields_are_bounded(self):
        c = collector.Collector.__new__(collector.Collector)
        c.emit = lambda stream,value: setattr(c,'value',value)
        c.event('filesystems','mounts',{'items':['x'*1000]*50})
        self.assertTrue(c.value['fields']['truncated'])
        self.assertLess(len(collector.json.dumps(c.value['fields']).encode()),16384)

    def test_config(self):
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w') as f:
            f.write('SUPPORT_INGEST_URL=https://example/ingest\nSUPPORT_APP_ID=a\nSUPPORT_APP_SECRET="a b"\nSUPPORT_SERVICES=a.service,b.service\n'); f.flush()
            config = collector.load_env(f.name)
            self.assertEqual(config['SUPPORT_APP_SECRET'],'a b')
            self.assertEqual(config['SUPPORT_SERVICES'],['a.service','b.service'])

if __name__ == '__main__': unittest.main()

class DeliveryTests(unittest.TestCase):
    def test_http_body_and_cursor_after_success(self):
        import http.server
        import json
        import queue
        import tempfile
        import threading
        import time
        from pathlib import Path
        received = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(200); self.end_headers()
            def log_message(self, *args): pass
        server = http.server.HTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory, patch.object(collector,'STATE',Path(directory)):
                c = collector.Collector.__new__(collector.Collector)
                c.config = {'SUPPORT_INGEST_URL':f'http://127.0.0.1:{server.server_port}/exact','SUPPORT_APP_ID':'id','SUPPORT_APP_SECRET':'secret'}
                c.pending = queue.Queue(); c.stop = threading.Event(); c.errors = {}; c.failed = {}; c.health_checks = {'services':[('unhealthy','web.service: failed, exit=1')]}; c.state = {}
                c.pending.put(({'stream':'uptime_seconds','timestamp':collector.timestamp(),'value':1},'cursor-1'))
                thread = threading.Thread(target=c.deliver,daemon=True); thread.start()
                deadline = time.monotonic()+3
                while not (Path(directory)/'state.json').exists() and time.monotonic()<deadline: time.sleep(.01)
                c.stop.set(); thread.join(2)
                self.assertEqual(received[0][0],'/exact')
                self.assertEqual(received[0][1]['Authorization'],'Bearer secret')
                self.assertEqual(received[0][1]['X-App-ID'],'id')
                self.assertNotIn('instance_id',received[0][2])
                self.assertEqual(received[0][2]['health'],{'status':'unhealthy','message':'web.service: failed, exit=1'})
                self.assertEqual(json.loads((Path(directory)/'state.json').read_text())['cursor'],'cursor-1')
        finally:
            server.shutdown(); server.server_close()

class TLSDeliveryTests(unittest.TestCase):
    def test_self_signed_https_requires_opt_out(self):
        import http.server
        import queue
        import shutil
        import ssl
        import subprocess
        import tempfile
        import threading
        import time
        from pathlib import Path
        if not shutil.which('openssl'):
            self.skipTest('openssl is required to create a test certificate')
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory)/'cert.pem', Path(directory)/'key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost'],check=True,capture_output=True)
            received = []
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_POST(self):
                    received.append(self.path)
                    self.send_response(200); self.end_headers()
                def log_message(self, *args): pass
            server = http.server.HTTPServer(('127.0.0.1',0),Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert,key)
            server.socket = context.wrap_socket(server.socket,server_side=True)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                with patch.object(collector,'STATE',Path(directory)):
                    c = collector.Collector.__new__(collector.Collector)
                    c.config = {'SUPPORT_INGEST_URL':f'https://localhost:{server.server_port}/ingest','SUPPORT_APP_ID':'id','SUPPORT_APP_SECRET':'secret','SUPPORT_TLS_VERIFY':False}
                    c.pending = queue.Queue(); c.stop = threading.Event(); c.errors = {}; c.failed = {}; c.health_checks = {}; c.state = {}
                    thread = threading.Thread(target=c.deliver,daemon=True); thread.start()
                    deadline = time.monotonic()+3
                    while not received and time.monotonic()<deadline: time.sleep(.01)
                    c.stop.set(); thread.join(2)
                    self.assertEqual(received,['/ingest'])
                    with self.assertRaises(collector.urllib.error.URLError):
                        collector.urllib.request.urlopen(c.config['SUPPORT_INGEST_URL'],timeout=2)
            finally:
                server.shutdown(); server.server_close()
