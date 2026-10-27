#!/usr/bin/env python3
"""Linux telemetry collector for HyperHive Support; Python standard library only."""
import datetime as dt
import glob
import json
import logging
import os
import queue
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get('STATE_DIRECTORY', str(ROOT / 'state')))
LIMIT = 15 * 1024
STREAMS = {name: {'name': name, 'type': kind, **({'unit': unit} if unit else {}), 'label': label} for name, kind, unit, label in [
 ('machine_info','text',None,'Machine information'), ('uptime_seconds','number','s','Uptime'),
 ('cpu_usage_percent','number','%','CPU usage'), ('memory_usage_percent','number','%','Memory usage'),
 ('root_usage_percent','number','%','Root filesystem usage'),
 ('network_receive_bytes_per_second','number','bytes/s','Network receive'), ('network_transmit_bytes_per_second','number','bytes/s','Network transmit'),
 *[(x,'log',None,x.replace('_',' ').title()) for x in ('journal_logs','system_logs','kernel_logs','service_logs')],
 *[(x,'event',None,x.replace('_',' ').title()) for x in ('service_failures','machine_events','filesystems','network_interfaces','top_processes','services','hardware')]]}


def read(path):
    return Path(path).read_text(errors='replace').strip()


def bounded(value, limit=LIMIT):
    return value.encode('utf-8', errors='replace')[:limit].decode('utf-8', errors='ignore')


def timestamp(seconds=None):
    return dt.datetime.fromtimestamp(time.time() if seconds is None else seconds, dt.timezone.utc).isoformat().replace('+00:00','Z')


def load_env(path):
    config = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep or not key.strip():
            raise ValueError('invalid .env line')
        parts = shlex.split(value, comments=True)
        config[key.strip()] = parts[0] if parts else ''
    for key in ('SUPPORT_INGEST_URL','SUPPORT_APP_ID','SUPPORT_APP_SECRET'):
        if not config.get(key):
            raise ValueError(key + ' is required')
    for key in ('SUPPORT_SERVICES','SUPPORT_NETWORK_INTERFACES'):
        config[key] = [x.strip() for x in config.get(key,'').split(',') if x.strip()]
    return config


def command(args, timeout=8):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError('%s failed: %s' % (args[0], bounded(result.stderr.strip(), 300)))
    return result.stdout


def cpu_ticks():
    values = [int(x) for x in read('/proc/stat').splitlines()[0].split()[1:9]]
    return sum(values), values[3] + values[4]


def cpu_percent(old, new):
    total, idle = new[0]-old[0], new[1]-old[1]
    if total <= 0 or idle < 0 or idle > total:
        return None
    return 100 * (total-idle)/total


def rate(old, new, elapsed):
    if elapsed <= 0 or new < old:
        return None
    return (new-old)/elapsed


def memory():
    data = {line.split(':',1)[0]: int(line.split()[1])*1024 for line in read('/proc/meminfo').splitlines() if len(line.split()) >= 2 and line.split()[1].isdigit()}
    return data['MemTotal'], data['MemAvailable']


def interfaces(configured):
    if configured:
        return configured
    # Default routes identify uplinks without counting bridge members and veth peers.
    names = set()
    try:
        for line in read('/proc/net/route').splitlines()[1:]:
            fields = line.split()
            if fields[1] == '00000000' and int(fields[3],16) & 1:
                names.add(fields[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        for line in read('/proc/net/ipv6_route').splitlines():
            fields = line.split()
            if fields[0] == '0'*32 and fields[1] == '00':
                names.add(fields[-1])
    except OSError:
        pass
    return sorted(n for n in names if n != 'lo' and not n.startswith(('veth','docker','br-')))


def network(names):
    result = {}
    for name in names:
        try:
            base = Path('/sys/class/net') / name
            if '/' in name or name in ('.','..'):
                raise ValueError('invalid interface')
            result[name] = {k: int(read(base / 'statistics' / k)) for k in ('rx_bytes','tx_bytes','rx_errors','tx_errors','rx_dropped','tx_dropped')}
            result[name]['state'] = read(base / 'operstate')
        except (OSError, ValueError) as exc:
            logging.warning('network interface %s: %s', name, exc)
    return result


def processes():
    result = {}
    for p in glob.glob('/proc/[0-9]*/stat'):
        try:
            line = read(p); start = line.index('('); end = line.rindex(')'); fields = line[end+2:].split()
            pid = int(line[:start].strip())
            result[pid] = {'pid':pid,'name':line[start+1:end], 'ticks':int(fields[11])+int(fields[12]), 'start':int(fields[19]), 'resident_memory_bytes':max(0,int(fields[21]))*os.sysconf('SC_PAGE_SIZE')}
        except (OSError, ValueError, IndexError):
            continue
    return result


def top_processes(old, new, elapsed):
    items = []
    for pid, proc in new.items():
        previous = old.get(pid)
        if not previous or proc['start'] != previous['start']:
            continue
        usage = rate(previous['ticks'], proc['ticks'], elapsed)
        if usage is None:
            continue
        items.append({'pid':pid,'name':proc['name'],'cpu_percent':100*usage/os.sysconf('SC_CLK_TCK'),'resident_memory_bytes':proc['resident_memory_bytes']})
    selected = {p['pid']:p for p in sorted(items,key=lambda p:p['cpu_percent'],reverse=True)[:10] + sorted(items,key=lambda p:p['resident_memory_bytes'],reverse=True)[:10]}
    return list(selected.values())


def machine_info():
    release = {}
    for line in read('/etc/os-release').splitlines():
        key, sep, val = line.partition('=')
        if sep:
            release[key] = val.strip('"\'')
    cpu = read('/proc/cpuinfo')
    model = next((line.split(':',1)[1].strip() for line in cpu.splitlines() if line.startswith(('model name','Hardware')) and ':' in line), 'unavailable')
    total, _ = memory()
    boot = next(line.split()[1] for line in read('/proc/stat').splitlines() if line.startswith('btime '))
    u = os.uname()
    return bounded(json.dumps({'hostname':socket.gethostname(),'distribution':release.get('PRETTY_NAME',release.get('NAME','unknown')),'kernel':u.release,'architecture':u.machine,'cpu_model':model,'cpu_cores':os.cpu_count(),'ram_bytes':total,'boot_time':timestamp(int(boot))},ensure_ascii=False))


def filesystems():
    items = []
    for line in read('/proc/self/mounts').splitlines():
        f = line.split()
        if len(f)<4 or not f[0].startswith('/dev/'):
            continue
        device, mount, kind = [x.replace('\\040',' ').replace('\\011','\t').replace('\\134','\\') for x in f[:3]]
        try:
            s = os.statvfs(mount)
            items.append({'device':device,'mount_point':mount,'filesystem_type':kind,'capacity_bytes':s.f_blocks*s.f_frsize,'free_bytes':s.f_bfree*s.f_frsize,'available_bytes':s.f_bavail*s.f_frsize,'inodes_total':s.f_files,'inodes_free':s.f_ffree})
        except OSError:
            continue
    return items


def system_services(configured):
    output = command(['systemctl','list-units','--failed','--all','--plain','--no-legend','--no-pager'])
    names = set(configured)
    names.update(line.split()[0] for line in output.splitlines() if line.split() and line.split()[0].endswith('.service'))
    result = []
    for name in sorted(names):
        raw = command(['systemctl','show',name,'--no-pager','--property=Id,LoadState,ActiveState,SubState,Result,ExecMainStatus,NRestarts'])
        d = dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
        result.append({'unit':d.get('Id',name),'load_state':d.get('LoadState','unknown'),'active_state':d.get('ActiveState','unknown'),'substate':d.get('SubState','unknown'),'result':d.get('Result','unknown'),'exit_code':d.get('ExecMainStatus','unknown'),'restart_count':d.get('NRestarts','unknown')})
    return result


def hardware():
    sensors = []
    for p in glob.glob('/sys/class/hwmon/hwmon*/temp*_input') + glob.glob('/sys/class/hwmon/hwmon*/fan*_input') + glob.glob('/sys/class/thermal/thermal_zone*/temp'):
        try:
            sensors.append({'sensor':p,'value':int(read(p)), 'unit':'millidegrees_celsius' if 'temp' in p else 'rpm'})
        except (OSError, ValueError):
            continue
    disks = []
    try:
        scan = command(['smartctl','--scan-open'],15)
        for line in scan.splitlines()[:32]:
            args = shlex.split(line.split('#',1)[0]);
            if not args: continue
            try:
                result = subprocess.run(['smartctl','-H','-j',*args],capture_output=True,text=True,timeout=15)
                data = json.loads(result.stdout)
                disks.append({'device':args[0], 'passed':data.get('smart_status',{}).get('passed'), 'available': 'passed' in data.get('smart_status',{})})
            except (OSError, ValueError, subprocess.TimeoutExpired):
                disks.append({'device':args[0], 'available':False})
        smart = 'available'
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        smart = 'unavailable'
    return {'sensors':sensors,'sensors_available':bool(sensors),'disk_health':disks,'smartctl':smart}


def journal_samples(entry, services):
    try: priority = int(entry.get('PRIORITY',6))
    except (ValueError,TypeError): priority = 6
    severity = 'fatal' if priority <= 2 else 'error' if priority == 3 else 'warn' if priority == 4 else 'info' if priority <= 6 else 'debug'
    unit = entry.get('_SYSTEMD_UNIT') or entry.get('SYSLOG_IDENTIFIER') or 'unknown'
    pid = entry.get('_PID') or entry.get('SYSLOG_PID')
    msg = entry.get('MESSAGE','')
    if not isinstance(msg,str): msg = str(msg)
    message = bounded('%s%s: %s' % (unit, '[%s]' % pid if pid else '', msg))
    try: stamp = timestamp(int(entry['__REALTIME_TIMESTAMP']) / 1000000)
    except (KeyError, ValueError, TypeError, OverflowError): stamp = timestamp()
    streams = ['journal_logs']
    if priority <= 4: streams.append('system_logs')
    if entry.get('_TRANSPORT') == 'kernel': streams.append('kernel_logs')
    if entry.get('_SYSTEMD_UNIT') in services or entry.get('SYSLOG_IDENTIFIER') in services: streams.append('service_logs')
    return [{'stream':s,'timestamp':stamp,'value':{'message':message,'severity':severity}} for s in streams]


class Collector:
    def __init__(self, config):
        self.config = config; STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.pending = queue.Queue(maxsize=2000); self.stop = threading.Event(); self.service_changed = threading.Event()
        self.errors = {}; self.failed = {}; self.previous_services = None
        self.old_cpu = None; self.old_net = {}; self.old_proc = {}; self.old_time = None
        self.last_info = None; self.mounts = None; self.links = None
        self.state = {}
        try: self.state = json.loads(read(STATE / 'state.json'))
        except (OSError, ValueError): pass
        self.session_boot = read('/proc/sys/kernel/random/boot_id')
        self.boot_event = bool(self.state.get('boot_id') and self.state['boot_id'] != self.session_boot)
        if not self.state.get('boot_id'):
            self.state['boot_id'] = self.session_boot
            self.save_state()

    def save_state(self):
        temp = STATE / 'state.tmp'
        temp.write_text(json.dumps(self.state)); os.chmod(temp,0o600); os.replace(temp, STATE / 'state.json')

    def emit(self, stream, value, stamp=None, cursor=None):
        item = {'stream':stream,'timestamp':stamp or timestamp(),'value':value}
        self.pending.put((item,cursor))

    def event(self, stream, name, fields):
        # Bound large snapshots by dropping trailing items; report the truncation.
        fields = dict(fields)
        if len(json.dumps(fields,ensure_ascii=False).encode()) > LIMIT and isinstance(fields.get('items'),list):
            fields['truncated'] = True
            while fields['items'] and len(json.dumps(fields,ensure_ascii=False).encode()) > LIMIT:
                fields['items'].pop()
        if len(json.dumps(fields,ensure_ascii=False).encode()) > LIMIT:
            fields = {'diagnostic':'fields exceeded size limit'}
        self.emit(stream, {'name':bounded(name,120),'fields':fields})

    def collect(self, name, fn):
        try:
            value = fn(); self.errors.pop(name,None); return value
        except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError, RuntimeError, subprocess.TimeoutExpired) as exc:
            message = '%s: %s' % (name, exc)
            if self.errors.get(name) != message:
                logging.warning('%s',message)
                self.emit('system_logs',{'message':bounded(message),'severity':'warn'})
            self.errors[name] = message
            return None

    def metrics(self):
        now = time.monotonic(); elapsed = now-self.old_time if self.old_time is not None else 0
        cpu = self.collect('cpu',cpu_ticks)
        if cpu and self.old_cpu:
            value = cpu_percent(self.old_cpu,cpu)
            if value is not None: self.emit('cpu_usage_percent',value)
        if cpu: self.old_cpu = cpu
        uptime = self.collect('uptime',lambda: float(read('/proc/uptime').split()[0]))
        if uptime is not None: self.emit('uptime_seconds',uptime)
        mem = self.collect('memory',memory)
        if mem and mem[0]>0: self.emit('memory_usage_percent',100*(mem[0]-mem[1])/mem[0])
        def root():
            s = os.statvfs('/'); used = s.f_blocks-s.f_bfree; return 100*used/(used+s.f_bavail)
        root_usage = self.collect('root filesystem',root)
        if root_usage is not None: self.emit('root_usage_percent',root_usage)
        net = self.collect('network',lambda: network(interfaces(self.config['SUPPORT_NETWORK_INTERFACES'])))
        self.net_rates = {}
        if net is not None:
            for name, item in net.items():
                old = self.old_net.get(name)
                if old and elapsed:
                    rx = rate(old['rx_bytes'],item['rx_bytes'],elapsed); tx = rate(old['tx_bytes'],item['tx_bytes'],elapsed)
                    if rx is not None and tx is not None: self.net_rates[name] = (rx,tx)
            if self.net_rates and len(self.net_rates)==len(net):
                self.emit('network_receive_bytes_per_second',sum(v[0] for v in self.net_rates.values()))
                self.emit('network_transmit_bytes_per_second',sum(v[1] for v in self.net_rates.values()))
            self.old_net = net
        proc = self.collect('processes',processes)
        if proc is not None:
            self.top = top_processes(self.old_proc,proc,elapsed) if elapsed else None
            self.old_proc = proc
        self.old_time = now
        info = self.collect('machine info',machine_info)
        if info and info != self.last_info: self.emit('machine_info',info); self.last_info = info

    def snapshots(self):
        mounts = self.collect('filesystems',filesystems)
        if mounts is not None:
            current = {(x['device'],x['mount_point'],x['filesystem_type']) for x in mounts}
            if self.mounts is not None and current != self.mounts: self.event('machine_events','Mounts changed',{'added':sorted(current-self.mounts),'removed':sorted(self.mounts-current)})
            self.mounts = current; self.event('filesystems',f'{len(mounts)} mounted filesystems',{'items':mounts})
        try: addresses = json.loads(command(['ip','-j','address','show']))
        except (OSError,RuntimeError,ValueError,subprocess.TimeoutExpired): addresses = None
        addr = {x['ifname']:[a['local']+'/'+str(a['prefixlen']) for a in x.get('addr_info',[]) if 'local' in a] for x in (addresses or [])}
        links = self.collect('network links',lambda: network([p.name for p in Path('/sys/class/net').iterdir() if p.name != 'lo']))
        if links is not None:
            current = {k:v['state'] for k,v in links.items()}
            if self.links is not None:
                for name in sorted(set(current)|set(self.links)):
                    if current.get(name) != self.links.get(name): self.event('machine_events','Network link changed',{'interface':name,'previous':self.links.get(name),'state':current.get(name)})
            self.links = current
            items = []
            for name, item in links.items():
                item = {'name':name,'link_state':item['state'],**({'ip_addresses':addr.get(name,[])} if addresses is not None else {}),**{k:item[k] for k in ('rx_errors','tx_errors','rx_dropped','tx_dropped')}}
                if name in self.net_rates: item.update(receive_bytes_per_second=self.net_rates[name][0],transmit_bytes_per_second=self.net_rates[name][1])
                items.append(item)
            self.event('network_interfaces',f'{len(items)} network interfaces',{'items':items})
        if getattr(self,'top',None) is not None: self.event('top_processes',f'{len(self.top)} top processes',{'items':self.top})
        services = self.collect('services',lambda: system_services(self.config['SUPPORT_SERVICES']))
        if services is not None:
            states = {s['unit']:s for s in services}; self.failed = {k:v for k,v in states.items() if v['active_state']=='failed' or v['load_state'] != 'loaded' or (k in self.config['SUPPORT_SERVICES'] and v['active_state'] not in ('active','activating'))}
            if self.previous_services is not None:
                for unit in sorted(set(self.previous_services)|set(states)):
                    previous = self.previous_services.get(unit); current = states.get(unit)
                    was_failed = bool(previous and previous['active_state']=='failed')
                    is_failed = bool(current and current['active_state']=='failed')
                    if was_failed != is_failed:
                        detail = current or previous; self.event('service_failures',unit+(' failed' if is_failed else ' recovered'),{**detail,'state':'failed' if is_failed else 'recovered','diagnostic_message':f"{unit} {detail['active_state']} ({detail['result']})"})
            else:
                for unit, detail in states.items():
                    if detail['active_state']=='failed': self.event('service_failures',unit+' failed',{**detail,'state':'failed','diagnostic_message':f"{unit} failed ({detail['result']})"})
            self.previous_services = states
            self.event('services',f'{len(services)} services',{'items':services})
        hw = self.collect('hardware',hardware)
        if hw is not None: self.event('hardware','Hardware status',hw)
        if self.boot_event:
            self.event('machine_events','Machine rebooted',{'boot_id':self.session_boot}); self.boot_event = False

    def journal(self):
        while not self.stop.is_set():
            args = ['journalctl','--follow','--output=json','--no-pager','--quiet']
            cursor = self.state.get('cursor')
            args += ['--after-cursor',cursor] if cursor else ['--since','-5 minutes']
            try:
                proc = subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                for line in proc.stdout:
                    if self.stop.is_set(): break
                    try:
                        entry = json.loads(line); self.errors.pop('journal',None); samples = journal_samples(entry,self.config['SUPPORT_SERVICES']); cursor = entry.get('__CURSOR')
                        if entry.get('SYSLOG_IDENTIFIER') == 'systemd' and any(word in str(entry.get('MESSAGE','')).lower() for word in ('failed', 'failure', 'started', 'recovered')):
                            self.service_changed.set()
                        for i, sample in enumerate(samples): self.pending.put((sample,cursor if i == len(samples)-1 else None))
                    except (ValueError,TypeError): continue
                proc.terminate(); proc.wait(timeout=2)
                self.errors['journal'] = 'journal reader stopped; check journal permissions and cursor'
                logging.error('%s',self.errors['journal'])
            except (OSError,subprocess.TimeoutExpired) as exc:
                self.errors['journal'] = 'journal reader: '+str(exc); logging.error('%s',self.errors['journal'])
            self.stop.wait(5)

    def deliver(self):
        batch = []; size = 0; backoff = 1; next_heartbeat = 0; carry = None
        while not self.stop.is_set():
            heartbeat = time.monotonic() >= next_heartbeat
            try:
                while len(batch)<900:
                    item = carry if carry is not None else self.pending.get(timeout=0.1 if not batch and not heartbeat else 0.001)
                    carry = None
                    n = len(json.dumps(item[0],ensure_ascii=True).encode())
                    if size+n > 800000:
                        carry = item; break
                    batch.append(item); size += n
            except queue.Empty: pass
            if not batch and not heartbeat: continue
            status = 'unhealthy' if self.failed else 'degraded' if self.errors else 'healthy'
            msg = '; '.join(list(self.errors.values()) + ['service '+x+' is not healthy' for x in self.failed])[:1024]
            body = {'heartbeat':heartbeat,'streams':list(STREAMS.values()),'samples':[x[0] for x in batch],'health':{'status':status,'message':msg}}
            request = urllib.request.Request(self.config['SUPPORT_INGEST_URL'],data=json.dumps(body,ensure_ascii=True).encode(),headers={'X-App-ID':self.config['SUPPORT_APP_ID'],'Authorization':'Bearer '+self.config['SUPPORT_APP_SECRET'],'Content-Type':'application/json'},method='POST')
            try:
                with urllib.request.urlopen(request,timeout=10) as response:
                    if not 200 <= response.status < 300: raise RuntimeError('HTTP '+str(response.status))
                for sample, cursor in batch:
                    if cursor: self.state['cursor'] = cursor
                    if sample['stream'] == 'machine_events' and sample['value'].get('name') == 'Machine rebooted':
                        self.state['boot_id'] = sample['value']['fields']['boot_id']
                self.save_state(); batch = []; size = 0; backoff = 1
                if heartbeat: next_heartbeat = time.monotonic()+15
            except (OSError,urllib.error.HTTPError,RuntimeError) as exc:
                code = getattr(exc,'code',None)
                logging.error('Support delivery failed%s: %s', ' (authentication or validation)' if code in (400,401,403,409,413,422) else '', exc)
                self.stop.wait(backoff); backoff = min(backoff*2,60)
            if batch or carry is not None or not self.pending.empty(): self.stop.wait(1)

    def run(self):
        threading.Thread(target=self.journal,daemon=True).start()
        threading.Thread(target=self.deliver,daemon=True).start()
        next_metrics = next_snapshots = 0
        while True:
            now = time.monotonic()
            if now >= next_metrics: self.metrics(); next_metrics = now+15
            if now >= next_snapshots or self.service_changed.is_set():
                self.service_changed.clear(); self.snapshots(); next_snapshots = time.monotonic()+60
            time.sleep(1)


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    try: Collector(load_env(os.environ.get('COLLECTOR_ENV',str(ROOT / '.env')))).run()
    except (OSError,ValueError) as exc:
        logging.error('collector startup failed: %s',exc); sys.exit(1)


if __name__ == '__main__': main()
