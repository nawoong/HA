# -*- coding: utf-8 -*-
'''
Kocom 월패드 RS485 <-> Home Assistant(MQTT) 브리지 - 난방(thermostat), 환기(fan) 전용

  python -m pip install pyserial paho-mqtt

구성
  load_config() : /data/options.json(HA 애드온) 또는 rs485.conf(단독 실행) 읽기
  Transport     : 소켓/시리얼 연결. 끊기면 RECONNECT_SEC 후 자동 재연결
  Unit          : 난방 방 하나 또는 환기장치 하나의 상태와 "보내야 할 명령"
  Kocom         : 패킷 수신/해석(read_loop), 조회/명령 전송(scan_loop), MQTT/HA 연동

코콤 패킷 (21 bytes = hex 42자)
  [0:4]   aa55          헤더
  [4:7]   30b / 30d     타입 send / ack
  [10:14] 장치2+방2     send: 목적지,  ack: 출발지
  [14:18] 장치2+방2     send: 출발지,  ack: 목적지
  [18:20] 명령          3a=조회, 00=상태(설정), 01=on, 02=off
  [20:36] 값 8 bytes
  [36:38] 체크섬        (앞 18 bytes 합 + 1) % 256
  [38:42] 0d0d
'''
import configparser
import json
import logging
import logging.handlers
import os
import socket
import threading
import time
from collections import namedtuple

import paho.mqtt.client as mqtt

try:
    import serial
except ImportError:  # 소켓만 쓰는 경우 pyserial 없어도 동작
    serial = None

SW_VERSION = 'RS485 Compilation 1.2.1'

ROOT_DIR = os.path.dirname(os.path.realpath(__file__))
OPTIONS_FILE = '/data/options.json'
CONF_PATH = os.path.join(ROOT_DIR, 'rs485.conf')
LOG_PATH = os.path.join(ROOT_DIR, 'log', 'rs485.log')

logger = logging.getLogger('RS485')

###############################################################################
# 프로토콜 상수
###############################################################################
DEV_WALLPAD = '01'
DEV_THERMOSTAT = '36'
DEV_FAN = '48'
DEVICE_NAMES = {'01': 'wallpad', '0e': 'light', '36': 'thermostat', '3b': 'plug',
                '44': 'elevator', '2c': 'gas', '48': 'fan'}          # 로그 표시용
CMD_QUERY = '3a'
CMD_STATE = '00'
COMMAND_NAMES = {'3a': '조회', '00': '상태', '01': 'on', '02': 'off'}
TYPE_NAMES = {'30b': 'send', '30d': 'ack'}
EMPTY_VALUE = '0' * 16

THERMO_MODES = ('off', 'heat', 'fan_only')         # fan_only = 외출
# off는 '0001': 월패드에서 직접 껐을 때 온도조절기가 보고한 값. '0100'은 ACK만 오고 꺼지지 않음
THERMO_MODE_CODE = {'heat': '1100', 'off': '0001', 'fan_only': '1101'}

FAN_SPEED = {'4': 'low', '8': 'medium', 'c': 'high', '0': 'off'}
FAN_SPEED_CODE = {v: k for k, v in FAN_SPEED.items()}
FAN_ROOM = '00'

# 이 집의 환기장치는 코콤 표준 환기 패킷으로 동작하지 않아 전용 패킷으로 대체해서 보낸다. (c5ba0ad)
# key = 표준 패킷이었다면 들어갔을 값(value) 부분
FAN_RAW_PACKETS = {
    '0001000000000000': 'aae10120020201006b55aa01e12082020100eb55',  # off
    '1100800000000000': 'aae10120020202036b55aa01e12082020203eb55',  # on (medium)
}

PACKET_HEX_LEN = 42

###############################################################################
# 타이밍
###############################################################################
BUS_IDLE_SEC = 0.1      # 마지막 송수신 후 이만큼 조용해야 송신
LOOP_SEC = 0.2          # scan_loop 주기
RETRY_SEC = 1.0         # 명령 재전송 간격
MAX_SENDS = 5           # 명령 최대 전송 횟수 (응답 없으면 포기)
SCAN_RETRY_SEC = 2.0    # 조회 재시도 간격
MAX_SCANS = 5           # 조회 최대 횟수 (응답 없으면 다음 SCAN_INTERVAL까지 대기)
RECONNECT_SEC = 10      # 연결 끊김 후 재연결 간격

###############################################################################
# Home Assistant
###############################################################################
HA_PREFIX = 'homeassistant'
HA_STATUS_TOPIC = 'homeassistant/status'
BRIDGE_TOPIC = 'rs485/bridge/config'
ENTITY_PREFIX = 'kocom'  # 엔티티 이름/unique_id 접두사. 바꾸면 HA에 엔티티가 새로 생김

###############################################################################
# 설정
###############################################################################
DEFAULT_OPTIONS = {
    'RS485': {'type': 'Socket'},
    'Socket': {'server': '', 'port': 8899},
    'Serial': {'port1': '/dev/ttyUSB0'},
    'MQTT': {'anonymous': False, 'server': '', 'port': 1883, 'username': '', 'password': ''},
    'Wallpad': {'thermostat': False, 'fan': False},
    'Advanced': {'INIT_TEMP': 22, 'SCAN_INTERVAL': 300, 'SCANNING_INTERVAL': 0.8,
                 'DEFAULT_SPEED': 'medium', 'LOGLEVEL': 'info'},
    # 순서대로 방 패킷 00, 01, 02 ...
    'KOCOM_ROOM_THERMOSTAT': ['livingroom', 'bedroom', 'room1', 'room2'],
}


def to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == 'true'


def load_config():
    '''HA 애드온이면 /data/options.json, 아니면 makeconf.sh 형식의 rs485.conf를 읽는다.'''
    cfg = json.loads(json.dumps(DEFAULT_OPTIONS))
    if os.path.isfile(OPTIONS_FILE):
        with open(OPTIONS_FILE) as f:
            src = json.load(f)
    elif os.path.isfile(CONF_PATH):
        parser = configparser.ConfigParser()
        parser.read(CONF_PATH)
        src = {section: dict(parser.items(section)) for section in parser.sections()}
    else:
        src = {}
    for key, value in src.items():
        if isinstance(cfg.get(key), dict) and isinstance(value, dict):
            cfg[key].update(value)
        elif key in cfg:
            cfg[key] = value
    return cfg

###############################################################################
# 패킷
###############################################################################
Frame = namedtuple('Frame', 'type src_dev src_room dst_dev dst_room cmd value')


def checksum(packet):
    return '{:02x}'.format((sum(bytes.fromhex(packet[:36])) + 1) % 256)


def is_valid(packet):
    return len(packet) >= PACKET_HEX_LEN and checksum(packet) == packet[36:38]


def parse_frame(packet):
    ptype = TYPE_NAMES.get(packet[4:7])
    first, second = packet[10:14], packet[14:18]
    if ptype == 'send':
        dst, src = first, second
    elif ptype == 'ack':
        src, dst = first, second
    else:
        return None
    return Frame(ptype, src[:2], src[2:], dst[:2], dst[2:], packet[18:20], packet[20:36])


def build_packet(device, room, cmd, value):
    body = 'aa5530bc00' + device + room + DEV_WALLPAD + '00' + cmd + value
    return body + checksum(body) + '0d0d'


def parse_thermostat(value, prev_target, init_temp):
    '''난방 응답 값 -> {'current_temp', 'mode', 'target_temp'}
    끔/외출 상태에서는 설정온도가 오지 않으므로 직전 설정온도를 유지한다.'''
    heating = value[0:2] == '11'
    away = value[2:4] == '01'
    keep_target = int(prev_target) if prev_target else init_temp
    result = {'current_temp': int(value[8:10], 16)}
    if heating and away:
        result['mode'] = 'fan_only'
        result['target_temp'] = keep_target
    elif heating:
        result['mode'] = 'heat'
        result['target_temp'] = int(value[4:6], 16)
    else:
        result['mode'] = 'off'
        result['target_temp'] = keep_target
    return result

###############################################################################
# 연결
###############################################################################
class Transport:
    '''소켓/시리얼 연결. read()/write()는 예외를 밖으로 내보내지 않고, 에러가 나면
    연결을 닫은 뒤 다음 read() 때 RECONNECT_SEC 간격으로 다시 연결한다.'''

    def __init__(self, cfg):
        self.kind = str(cfg['RS485']['type']).lower()
        self.server = cfg['Socket'].get('server')
        self.port = cfg['Socket'].get('port')
        self.serial_port = cfg['Serial'].get('port1')
        self.conn = None
        self.next_retry = 0.0
        self.lock = threading.Lock()

    @property
    def is_open(self):
        return self.conn is not None

    @property
    def target(self):
        return '{}:{}'.format(self.server, self.port) if self.kind == 'socket' else self.serial_port

    def _open(self):
        if self.kind == 'socket':
            conn = socket.create_connection((self.server, int(self.port)), timeout=10)
            # 변환기(EW11 등)가 전원/WiFi 문제로 조용히 끊겨도 약 1분 안에 에러가 나게 함
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, 'TCP_KEEPIDLE'):
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            conn.settimeout(1.0)  # 무한 대기하지 않도록
            return conn
        if serial is None:
            raise RuntimeError('pyserial이 설치되어 있지 않습니다')
        return serial.Serial(self.serial_port, 9600, bytesize=8, stopbits=1, timeout=1)

    def ensure_open(self):
        with self.lock:
            if self.conn is not None:
                return True
            if time.time() < self.next_retry:
                return False
            try:
                self.conn = self._open()
                logger.info('[Connect] 연결 성공 [{}]'.format(self.target))
                return True
            except Exception as e:
                logger.warning('[Connect] 연결 실패 [{}]: {} ({}초 후 재시도)'.format(self.target, e, RECONNECT_SEC))
                self.next_retry = time.time() + RECONNECT_SEC
                return False

    def close(self, reason):
        with self.lock:
            if self.conn is None:
                return
            logger.warning('[Connect] 연결 끊김 [{}]: {} ({}초 후 재연결)'.format(self.target, reason, RECONNECT_SEC))
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
            self.next_retry = time.time() + RECONNECT_SEC

    def read(self):
        '''받은 bytes. 데이터가 없거나(최대 1초 대기) 연결이 안 되어 있으면 b''.'''
        if not self.ensure_open():
            time.sleep(0.5)
            return b''
        conn = self.conn
        try:
            if self.kind == 'socket':
                try:
                    data = conn.recv(64)
                except socket.timeout:
                    return b''
                if not data:
                    raise ConnectionError('상대방이 연결을 닫음')
                return data
            return conn.read(max(1, conn.in_waiting))
        except Exception as e:
            self.close(e)
            return b''

    def write(self, data):
        conn = self.conn
        if conn is None:
            return False
        try:
            if self.kind == 'socket':
                conn.sendall(data)
            else:
                conn.write(data)
            return True
        except Exception as e:
            self.close(e)
            return False

###############################################################################
# 장치
###############################################################################
class Unit:
    '''난방 방 하나 또는 환기장치 하나.
    state   : 월패드에서 확인한 상태
    desired : HA가 요청한 상태. pending이면 월패드에 반영될 때까지(최대 MAX_SENDS회) 전송'''

    def __init__(self, kind, name, device, room_code, state, needs_ack):
        self.kind = kind            # 'thermostat' | 'fan'
        self.name = name            # HA 토픽에 쓰이는 이름 (livingroom, wallpad ...)
        self.device = device
        self.room_code = room_code
        self.state = dict(state)
        self.desired = dict(state)
        self.needs_ack = needs_ack  # False면 응답 확인 없이 MAX_SENDS회 전송
        self.pending = False
        self.sends = 0
        self.sent_at = 0.0
        self.reset_scan()

    def request(self, **changes):
        self.desired.update(changes)
        self.pending = True
        self.sends = 0
        self.sent_at = 0.0

    def reset_scan(self):
        self.scan_tick = 0.0     # 마지막으로 상태를 확인한 시각
        self.scan_sent_at = 0.0
        self.scans = 0

    def mark_scanned(self):
        self.scan_tick = time.time()
        self.scan_sent_at = 0.0
        self.scans = 0

###############################################################################
# 코콤
###############################################################################
class Kocom:
    def __init__(self, cfg, transport):
        adv = cfg['Advanced']
        self.init_temp = int(adv['INIT_TEMP'])
        self.scan_interval = float(adv['SCAN_INTERVAL'])
        self.scanning_interval = float(adv['SCANNING_INTERVAL'])
        self.default_speed = adv['DEFAULT_SPEED']
        if self.default_speed not in ('low', 'medium', 'high'):
            logger.warning('[Config] DEFAULT_SPEED 설정오류로 medium 으로 설정. {} -> medium'.format(self.default_speed))
            self.default_speed = 'medium'

        self.transport = transport
        self.lock = threading.RLock()
        self.last_activity = 0.0
        self.mqtt_ready = threading.Event()

        self.thermostats = {}   # 방이름 -> Unit
        self.thermo_rooms = {}  # 방코드 -> 방이름
        if to_bool(cfg['Wallpad'].get('thermostat')):
            for i, room in enumerate(cfg['KOCOM_ROOM_THERMOSTAT']):
                code = '{:02d}'.format(i)
                self.thermo_rooms[code] = room
                self.thermostats[room] = Unit(
                    'thermostat', room, DEV_THERMOSTAT, code,
                    {'mode': 'off', 'current_temp': 0, 'target_temp': self.init_temp}, needs_ack=True)
        self.fan = None
        if to_bool(cfg['Wallpad'].get('fan')):
            # 전용 패킷(FAN_RAW_PACKETS)에 대한 응답은 해석할 수 없으므로 응답 확인 없이 반복 전송
            self.fan = Unit('fan', 'wallpad', DEV_FAN, FAN_ROOM, {'mode': 'off', 'speed': 'off'}, needs_ack=False)

        self.mqtt_conf = cfg['MQTT']
        self.mqtt = self.make_mqtt(self.mqtt_conf)

    def units(self):
        units = list(self.thermostats.values())
        if self.fan:
            units.append(self.fan)
        return units

    def run(self):
        logger.info('[Config] 연결 {} [{}], 난방 {}, 환기 {}'.format(
            self.transport.kind, self.transport.target, list(self.thermostats) or '사용안함',
            '사용' if self.fan else '사용안함'))
        self.mqtt.connect_async(self.mqtt_conf['server'], int(self.mqtt_conf.get('port') or 1883), 60)
        self.mqtt.loop_start()  # 끊기면 paho가 자동 재연결
        threading.Thread(target=self.read_loop, name='kocom-read', daemon=True).start()
        self.scan_loop()

    # ---------------------------------------------------------------- 수신
    def read_loop(self):
        packet = ''
        while True:
            try:
                for byte in self.transport.read():
                    h = '{:02x}'.format(byte)
                    if not packet and h != 'aa':
                        continue
                    packet += h
                    if len(packet) >= PACKET_HEX_LEN:
                        if is_valid(packet):
                            self.last_activity = time.time()
                            self.handle_packet(packet)
                        packet = ''
            except Exception:
                logger.exception('[Read] 처리 중 예외, 패킷 버림: {}'.format(packet))
                packet = ''
                time.sleep(0.1)

    def room_name(self, device, room_code):
        if device == DEV_THERMOSTAT:
            return self.thermo_rooms.get(room_code, room_code)
        return room_code

    def handle_packet(self, packet, source='kocom'):
        frame = parse_frame(packet)
        if frame is None:
            logger.debug('[From {}]알 수 없는 패킷 {}'.format(source, packet))
            return
        logger.debug('[From {}]{} {}({}) {}({}) -> {}({}) = {}'.format(
            source, packet, frame.type, COMMAND_NAMES.get(frame.cmd, frame.cmd),
            DEVICE_NAMES.get(frame.src_dev, frame.src_dev), self.room_name(frame.src_dev, frame.src_room),
            DEVICE_NAMES.get(frame.dst_dev, frame.dst_dev), self.room_name(frame.dst_dev, frame.dst_room),
            frame.value))
        # 난방 응답만 상태로 반영한다 (c5ba0ad). 환기 응답은 무시.
        if frame.type == 'ack' and frame.src_dev == DEV_THERMOSTAT:
            self.on_thermostat_state(frame)

    def on_thermostat_state(self, frame):
        unit = self.thermostats.get(self.thermo_rooms.get(frame.src_room))
        if unit is None:
            return
        with self.lock:
            value = parse_thermostat(frame.value, unit.state['target_temp'], self.init_temp)
            unit.state.update(value)
            unit.mark_scanned()
            if unit.pending:
                if value['mode'] == unit.desired['mode'] and (
                        value['mode'] != 'heat' or value['target_temp'] == unit.desired['target_temp']):
                    unit.pending = False
            else:
                # 월패드에서 직접 바꾼 값도 따라감 -> 이후 HA 명령이 옛 값을 보내지 않도록
                unit.desired.update(mode=value['mode'], target_temp=value['target_temp'])
        logger.info('[From kocom]thermostat/{}/state = {}'.format(unit.name, value))
        self.publish_state(unit, value)

    # ---------------------------------------------------------------- 송신
    def scan_loop(self):
        while True:
            try:
                if self.mqtt_ready.is_set() and self.transport.is_open:
                    self.scan_once()
            except Exception:
                logger.exception('[Scan]Error')
            time.sleep(LOOP_SEC)

    def scan_once(self):
        for unit in self.units():
            now = time.time()
            if now - self.last_activity < BUS_IDLE_SEC:
                return  # 버스 사용 중. 다음 주기에 이어서
            if now - unit.scan_tick > self.scan_interval:
                # 상태 확인할 때가 됨: 응답이 올 때까지 SCAN_RETRY_SEC 간격으로 최대 MAX_SCANS회 조회
                if now - unit.scan_sent_at > SCAN_RETRY_SEC:
                    unit.scans += 1
                    unit.scan_sent_at = now
                    self.send(unit, build_packet(unit.device, unit.room_code, CMD_QUERY, EMPTY_VALUE), '조회')
                    time.sleep(self.scanning_interval)
                if unit.scans >= MAX_SCANS:
                    unit.scan_tick = now
                    unit.scan_sent_at = 0.0
                    unit.scans = 0
            else:
                self.process_pending(unit, now)

    def process_pending(self, unit, now):
        with self.lock:
            if not unit.pending:
                return
            if unit.sends >= MAX_SENDS:
                unit.pending = False
                if unit.needs_ack:
                    logger.warning('[To kocom]{}/{} 응답 없음, 명령 포기 {}'.format(unit.kind, unit.name, unit.desired))
                else:
                    unit.state.update(unit.desired)
                return
            if unit.sends and now - unit.sent_at <= RETRY_SEC:
                return
            unit.sends += 1
            unit.sent_at = now
            packet = self.command_packet(unit)
            desc = '{} ({}/{})'.format(', '.join('{}={}'.format(k, v) for k, v in sorted(unit.desired.items())
                                                 if k != 'current_temp'), unit.sends, MAX_SENDS)
        self.send(unit, packet, desc)

    def command_packet(self, unit):
        d = unit.desired
        if unit.kind == 'thermostat':
            value = THERMO_MODE_CODE[d['mode']] + '{:02x}'.format(int(d['target_temp'])) + '0' * 10
            return build_packet(unit.device, unit.room_code, CMD_STATE, value)
        value = ('1100' if d['mode'] == 'on' else '0001') + FAN_SPEED_CODE[d['speed']] + '0' * 11
        return FAN_RAW_PACKETS.get(value) or build_packet(unit.device, unit.room_code, CMD_STATE, value)

    def send(self, unit, packet, desc):
        logger.info('[To kocom]{}/{} -> {}'.format(unit.kind, unit.name, desc))
        logger.debug('[To kocom]{}'.format(packet))
        self.last_activity = time.time()
        self.transport.write(bytes.fromhex(packet))

    # ---------------------------------------------------------------- MQTT
    def make_mqtt(self, conf):
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)  # paho-mqtt 2.x
        except AttributeError:
            client = mqtt.Client()                                  # paho-mqtt 1.x
        if not to_bool(conf.get('anonymous')):
            if not conf.get('username') or not conf.get('password'):
                logger.error('[MQTT] username/password 설정을 확인하세요. Server[{}]'.format(conf.get('server')))
            else:
                client.username_pw_set(username=conf['username'], password=conf['password'])
        client.on_connect = self.on_connect
        client.on_message = self.on_message
        return client

    def on_connect(self, client, userdata, flags, rc):
        if int(rc) == 0:
            logger.info('[MQTT] connected OK')
            self.discovery()
            self.mqtt_ready.set()
            return
        reasons = {1: 'incorrect protocol version', 2: 'invalid client identifier', 3: 'server unavailable',
                   4: 'bad username or password', 5: 'not authorised'}
        logger.warning('[MQTT] {}: Connection refused - {}'.format(rc, reasons.get(int(rc), 'unknown')))

    @staticmethod
    def ha_device(name):
        return {'name': 'Kocom {}'.format(name), 'ids': 'kocom_{}'.format(name),
                'mf': 'KOCOM', 'mdl': 'Wallpad', 'sw': SW_VERSION}

    def discovery_configs(self):
        configs = []
        if self.fan:
            base = '{}/fan/wallpad'.format(HA_PREFIX)
            configs.append(('{}/fan/wallpad_fan/config'.format(HA_PREFIX), {
                'name': '{}_wallpad_fan'.format(ENTITY_PREFIX),
                'cmd_t': base + '/mode',
                'stat_t': base + '/state',
                'spd_cmd_t': base + '/speed',
                'spd_stat_t': base + '/state',
                'stat_val_tpl': '{{ value_json.mode }}',
                'spd_val_tpl': '{{ value_json.speed }}',
                'pl_on': 'on',
                'pl_off': 'off',
                'spds': ['low', 'medium', 'high', 'off'],
                'uniq_id': '{}_wallpad_fan'.format(ENTITY_PREFIX),
                'device': self.ha_device('wallpad'),
            }))
        for room in self.thermostats:
            base = '{}/climate/{}'.format(HA_PREFIX, room)
            configs.append((base + '/config', {
                'name': '{}_{}_thermostat'.format(ENTITY_PREFIX, room),
                'mode_cmd_t': base + '/mode',
                'mode_stat_t': base + '/state',
                'mode_stat_tpl': '{{ value_json.mode }}',
                'temp_cmd_t': base + '/target_temp',
                'temp_stat_t': base + '/state',
                'temp_stat_tpl': '{{ value_json.target_temp }}',
                'curr_temp_t': base + '/state',
                'curr_temp_tpl': '{{ value_json.current_temp }}',
                'min_temp': 5,
                'max_temp': 40,
                'temp_step': 1,
                'modes': list(THERMO_MODES),
                'uniq_id': '{}_{}_thermostat'.format(ENTITY_PREFIX, room),
                'device': self.ha_device(room),
            }))
        return configs

    def discovery(self, remove=False):
        subscribe = [(BRIDGE_TOPIC + '/#', 0), (HA_STATUS_TOPIC, 0)]
        for topic, payload in self.discovery_configs():
            for key in ('cmd_t', 'spd_cmd_t', 'mode_cmd_t', 'temp_cmd_t'):
                if key in payload:
                    subscribe.append((payload[key], 0))
            self.mqtt.publish(topic, '' if remove else json.dumps(payload))
        self.mqtt.subscribe(subscribe)

    def republish_states(self):
        for unit in self.thermostats.values():
            if unit.scan_tick:
                self.publish_state(unit, {k: unit.state[k] for k in ('mode', 'target_temp', 'current_temp')})
        if self.fan:
            self.publish_state(self.fan, dict(self.fan.desired))

    def publish_state(self, unit, value):
        topic = '{}/{}/{}/state'.format(HA_PREFIX, 'climate' if unit.kind == 'thermostat' else 'fan', unit.name)
        payload = json.dumps(value)
        self.mqtt.publish(topic, payload)
        logger.info('[To HA]{} = {}'.format(topic, payload))

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors='replace')
        parts = msg.topic.split('/')
        try:
            if msg.topic.startswith(BRIDGE_TOPIC + '/'):
                self.on_bridge(parts[3], payload)
            elif msg.topic == HA_STATUS_TOPIC:
                if payload == 'online':  # HA 재시작: 엔티티 등록과 현재 상태를 다시 보냄
                    logger.info('[From HA]HomeAssistant online')
                    self.discovery()
                    self.republish_states()
            elif len(parts) == 4 and parts[0] == HA_PREFIX and parts[1] == 'climate':
                self.on_climate(parts[2], parts[3], payload)
            elif len(parts) == 4 and parts[0] == HA_PREFIX and parts[1] == 'fan':
                self.on_fan(parts[3], payload)
        except Exception:
            logger.exception('[From HA]Error {} = {}'.format(msg.topic, payload))

    def on_climate(self, room, command, payload):
        unit = self.thermostats.get(room)
        if unit is None or command not in ('mode', 'target_temp'):
            return
        with self.lock:
            if command == 'target_temp':
                unit.request(target_temp=int(float(payload)), mode='heat')
            elif payload in THERMO_MODES:
                unit.request(mode=payload)
            else:
                logger.warning('[From HA]지원하지 않는 난방 모드: {}'.format(payload))
                return
            ha = {'mode': unit.desired['mode'], 'target_temp': unit.desired['target_temp'],
                  'current_temp': unit.state['current_temp']}
        logger.info('[From HA]thermostat/{}/set = [mode={}, target_temp={}]'.format(room, ha['mode'], ha['target_temp']))
        self.publish_state(unit, ha)  # 월패드 응답을 기다리지 않고 HA에 먼저 반영

    def on_fan(self, command, payload):
        if self.fan is None:
            return
        with self.lock:
            if command == 'speed' and payload in FAN_SPEED_CODE:
                self.fan.request(speed=payload, mode='on')
            elif command == 'mode' and payload in ('on', 'off'):
                self.fan.request(mode=payload, speed=self.default_speed if payload == 'on' else 'off')
            else:
                logger.warning('[From HA]지원하지 않는 환기 명령: {} = {}'.format(command, payload))
                return
            ha = dict(self.fan.desired)
        logger.info('[From HA]fan/wallpad/set = [mode={}, speed={}]'.format(ha['mode'], ha['speed']))
        self.publish_state(self.fan, ha)

    def on_bridge(self, command, payload):
        '''rs485/bridge/config/<command> 로 보내는 디버그용 명령'''
        logger.info('[From HA]{}/{} = {}'.format(BRIDGE_TOPIC, command, payload))
        if command == 'log_level':
            levels = {'debug': logging.DEBUG, 'info': logging.INFO, 'warn': logging.WARNING}
            if payload in levels:
                logger.setLevel(levels[payload])
        elif command == 'restart':
            self.discovery()
            self.republish_states()
        elif command == 'remove':
            self.discovery(remove=True)
        elif command == 'scan':
            with self.lock:
                for unit in self.units():
                    unit.reset_scan()
        elif command == 'packet':
            # 받은 것처럼 처리. 월패드->장치 조회 패킷이면 실제로 버스에 전송
            packet = payload.strip().lower().replace(' ', '')
            if len(packet) < PACKET_HEX_LEN:
                logger.warning('[From HA]패킷 길이 오류: {}'.format(packet))
                return
            self.handle_packet(packet, source='HA')
            frame = parse_frame(packet)
            if frame and frame.cmd == CMD_QUERY and frame.src_dev == DEV_WALLPAD:
                self.last_activity = time.time()
                self.transport.write(bytes.fromhex(packet))
        elif command == 'check_sum':
            packet = payload.strip().lower()
            logger.info('[From HA]{} = {}({})'.format(packet, is_valid(packet), checksum(packet)))

###############################################################################
def setup_logging(level_name):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s : Line %(lineno)s - %(message)s')
    file_handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1024 * 1024, backupCount=10, encoding='utf-8')
    stream_handler = logging.StreamHandler()  # HA 애드온 로그 화면에 표시
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    levels = {'debug': logging.DEBUG, 'info': logging.INFO, 'warn': logging.WARNING}
    logger.setLevel(levels.get(level_name, logging.INFO))


def main():
    cfg = load_config()
    setup_logging(str(cfg['Advanced'].get('LOGLEVEL', 'info')).lower())
    logger.info('{} 시작'.format(SW_VERSION))
    transport = Transport(cfg)
    if transport.kind not in ('socket', 'serial'):
        logger.error('[Config] RS485 type은 Serial 또는 Socket 이어야 합니다: {}'.format(transport.kind))
        return 1
    Kocom(cfg, transport).run()


if __name__ == '__main__':
    raise SystemExit(main() or 0)
