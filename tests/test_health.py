import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import collector


def service(**changes):
    item = dict(unit='web.service', configured=False, service_type='simple', load_state='loaded', active_state='active', substate='running', result='success', exit_code='0')
    item.update(changes)
    return item


class HealthTests(unittest.TestCase):
    def test_service_classifications(self):
        cases = [
            ({}, None),
            ({'active_state':'inactive'}, None),
            ({'load_state':'masked','active_state':'inactive'}, None),
            ({'load_state':'not-found','active_state':'inactive'}, None),
            ({'active_state':'failed','result':'exit-code','exit_code':'1'}, 'unhealthy'),
            ({'load_state':'bad-setting'}, 'unhealthy'),
            ({'configured':True,'active_state':'inactive'}, 'unhealthy'),
            ({'configured':True,'load_state':'not-found'}, 'unhealthy'),
            ({'configured':True,'load_state':'masked'}, 'unhealthy'),
            ({'configured':True,'service_type':'oneshot','active_state':'inactive'}, None),
            ({'configured':True,'service_type':'oneshot','active_state':'failed','result':'exit-code'}, 'unhealthy'),
            ({'active_state':'activating','substate':'auto-restart','result':'exit-code'}, 'degraded'),
            ({'configured':True,'active_state':'activating','substate':'start'}, None),
            ({'load_state':'unknown'}, 'degraded'),
            ({'active_state':'unknown'}, 'degraded'),
            ({'result':'exit-code','exit_code':'1'}, None),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                issue = collector.service_health(service(**changes))
                self.assertEqual(issue[0] if issue else None, expected)
                if issue:
                    self.assertIn('web.service',issue[1])
                    self.assertIn('result=',issue[1])

    def test_missing_unit_and_alias_are_reported_without_losing_other_services(self):
        def show(args, **kwargs):
            name = args[-1]
            if name == 'missing.service':
                return Mock(returncode=1,stdout='Id=missing.service\nLoadState=not-found\nActiveState=inactive\n',stderr='not found')
            return Mock(returncode=0,stdout='Id=real.service\nNames=alias.service real.service\nLoadState=loaded\nActiveState=inactive\n',stderr='')
        with patch.object(collector,'command',return_value=''), patch.object(collector.subprocess,'run',side_effect=show):
            services = collector.system_services(['alias.service','missing.service'])
        self.assertEqual(len(services),2)
        self.assertTrue(all(s['configured'] for s in services))
        self.assertTrue(all(collector.service_health(s)[0]=='unhealthy' for s in services))

    def test_systemctl_access_failure_is_not_a_missing_service(self):
        with patch.object(collector,'command',return_value=''), patch.object(collector.subprocess,'run',return_value=Mock(returncode=1,stdout='',stderr='permission denied')):
            with self.assertRaisesRegex(RuntimeError,'permission denied'):
                collector.system_services(['web.service'])

    def test_service_names_are_normalized(self):
        with tempfile.NamedTemporaryFile(mode='w') as config:
            config.write('SUPPORT_INGEST_URL=http://example/ingest\nSUPPORT_APP_ID=a\nSUPPORT_APP_SECRET=b\nSUPPORT_SERVICES=nginx,sshd.service\n')
            config.flush()
            self.assertEqual(collector.load_env(config.name)['SUPPORT_SERVICES'],['nginx.service','sshd.service'])

    def test_filesystem_space_and_inode_thresholds(self):
        base = dict(mount_point='/data',capacity_bytes=1000,available_bytes=200,inodes_total=100,inodes_free=50)
        for changes, expected in [({},None), ({'available_bytes':100},'degraded'), ({'available_bytes':21},'degraded'), ({'available_bytes':20},'unhealthy'), ({'available_bytes':-1},'unhealthy'), ({'inodes_free':0},'unhealthy'), ({'available_bytes':0,'read_only':True},None), ({'capacity_bytes':0,'inodes_total':0},None)]:
            with self.subTest(changes=changes):
                issues = collector.filesystem_health([{**base,**changes}])
                self.assertEqual(issues[0][0] if issues else None,expected)

    def test_filesystems_include_overlay_root_and_mark_read_only_mounts(self):
        stats = Mock(f_blocks=100, f_frsize=4096, f_bfree=50, f_bavail=45, f_files=100, f_ffree=50)
        mounts = 'overlay / overlay rw 0 0\n/dev/loop0 /snap/app squashfs ro 0 0\nproc /proc proc rw 0 0\n'
        with patch.object(collector,'read',return_value=mounts), patch.object(collector.os,'statvfs',return_value=stats):
            items = collector.filesystems()
        self.assertEqual([x['mount_point'] for x in items],['/','/snap/app'])
        self.assertFalse(items[0]['read_only'])
        self.assertTrue(items[1]['read_only'])

    def test_health_prioritizes_failures_and_bounds_utf8_message(self):
        c = collector.Collector.__new__(collector.Collector)
        c.health_checks = {'services':[('unhealthy','web.service failed')], 'hardware':[]}
        c.errors = {'journal':'é'*1500}
        health = c.health()
        self.assertEqual(health['status'],'unhealthy')
        self.assertTrue(health['message'].startswith('web.service failed'))
        self.assertLessEqual(len(health['message'].encode()),1024)
        c.health_checks['services'] = []
        self.assertEqual(c.health()['status'],'degraded')
        c.errors.clear()
        self.assertEqual(c.health(),{'status':'healthy','message':''})

    def test_snapshots_report_failures_recovery_and_retain_last_state_on_check_error(self):
        c = collector.Collector.__new__(collector.Collector)
        c.config = {'SUPPORT_SERVICES':['web.service'],'SUPPORT_NETWORK_INTERFACES':[]}
        c.pending = queue.Queue(); c.errors = {}; c.failed = {}; c.health_checks = {}
        c.mounts = c.links = None; c.net_rates = {}; c.boot_event = False
        with patch.object(collector,'filesystems',return_value=[]), patch.object(collector,'interfaces',return_value=[]), patch.object(collector,'network',return_value={}), patch.object(collector,'command',return_value='[]'), patch.object(collector,'system_services',return_value=[service(configured=True,active_state='inactive')]) as services, patch.object(collector,'hardware',return_value={'disk_health':[{'device':'/dev/sda','passed':False}]}) as hw:
            c.snapshots()
            health = c.health()
            self.assertEqual(health['status'],'unhealthy')
            self.assertIn('web.service',health['message'])
            self.assertIn('/dev/sda',health['message'])
            events = [item[0] for item in list(c.pending.queue) if item[0]['stream']=='service_failures']
            self.assertEqual(events[-1]['value']['fields']['state'],'failed')
            services.side_effect = RuntimeError('cannot read services')
            c.snapshots()
            self.assertEqual(c.health()['status'],'unhealthy')
            services.side_effect = None; services.return_value = [service(configured=True)]
            hw.return_value = {'disk_health':[{'device':'/dev/sda','available':False}]}
            c.snapshots()
            self.assertEqual(c.health(),{'status':'healthy','message':''})
            events = [item[0] for item in list(c.pending.queue) if item[0]['stream']=='service_failures']
            self.assertEqual([x['value']['fields']['state'] for x in events],['failed','recovered'])

    def test_startup_does_not_claim_healthy_before_initial_checks(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(collector,'STATE',Path(directory)):
            c = collector.Collector({})
            self.assertEqual(c.health()['status'],'degraded')
            self.assertIn('check pending',c.health()['message'])

    def test_delivery_sends_evaluated_health_and_heartbeat(self):
        c = collector.Collector.__new__(collector.Collector)
        c.config = {'SUPPORT_INGEST_URL':'http://localhost/ingest','SUPPORT_APP_ID':'id','SUPPORT_APP_SECRET':'secret'}
        c.pending = queue.Queue(); c.stop = threading.Event(); c.state = {}; c.errors = {}
        c.health_checks = {'hardware':[('unhealthy','disk /dev/sda: SMART health check failed')]}
        response = Mock(status=200)
        def send(request, **kwargs):
            body = json.loads(request.data)
            self.assertTrue(body['heartbeat'])
            self.assertEqual(body['health'],c.health())
            c.stop.set()
            return Mock(__enter__=Mock(return_value=response),__exit__=Mock(return_value=False))
        with patch.object(collector.urllib.request,'urlopen',side_effect=send), patch.object(c,'save_state'):
            c.deliver()
