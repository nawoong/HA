# Hass.io Add-on: Kocom Wallpad Controller with RS485 

![Supports aarch64 Architecture][aarch64-shield] ![Supports amd64 Architecture][amd64-shield] ![Supports armhf Architecture][armhf-shield] ![Supports armv7 Architecture][armv7-shield] ![Supports i386 Architecture][i386-shield]

## About
랜이님이 만든 코콤 월패드 프로그램을 애드온으로 만든 것입니다.

## Version : 1.2.0
- 난방(thermostat), 환기(fan) 전용으로 정리. 조명/콘센트/가스/엘리베이터, 그렉스 제거
- 소켓/시리얼 연결이 끊기면 자동 재연결, 수신 스레드가 예외로 죽지 않도록 수정
- HA 재시작(homeassistant/status = online) 시 엔티티 재등록 및 상태 재발행
- 로그를 애드온 로그 화면에도 출력

### Version : 1.1.1
- auto_uart deprecated. change to uart config
- alpine linux images를 python3 이미지로 교체
- CPU 최적화 및 딜레이 수정

## Installation

1. 홈어시스턴트의 Hass.io > ADD-ON STORE에서 Add new repository by URL에 https://github.com/HAKorea/addons 를 입력한 다음 ADD 버튼을 누릅니다.
2. ADD-ON STORE 페이지 하단에서 "Kocom Wallpad Controller with RS485" 클릭합니다.
3. "INSTALL" 버튼을 누르면 애드온이 설치됩니다. 최대 약 10분 정도 소요. 
4. INSTALL 버튼위에 설치 애니메이션이 동작하는데 이것이 멈추더라도 REBUILD, START 버튼이 나타나지 않는 경우가 있습니다.
5. 이 애드온은 이미지를 내려받는 것이 아니라 직접 여러분의 Hassio에서 이미지를 만듭니다. 따라서 컴퓨터성능과 인터넷 속도에 따라서 시간이 좀 걸립니다. 
6. INSTALL 버튼을 누른다음 설치 애니메이션이 실행되면 제대로 설치중인 것입니다. INSTALL을 여러번 누르지 마시고 기다리다 지치면 브라우저 페이지를 리프리시 하세요. 
7. 애드온 페이지에서 Config을 본인의 환경에 맞게 수정합니다.
8. "START" 버튼으로 애드온을 실행합니다.

만일 rs485.py 파일을 수정하시려면 한번 실행한 후 애드온을 Stop 하시고 share/kocom/ 폴더에 있는 파일을 알맞게 수정하신 다음 애드온을 Start 하시면 이후부터는 수정된 파일을 적용합니다.

> **업데이트 주의**: 애드온은 처음 실행할 때만 `rs485.py`를 `share/kocom/`에 복사합니다.
> 새 버전의 `rs485.py`를 적용하려면 애드온을 Stop 한 뒤 `share/kocom/rs485.py`를 지우고(또는 새 파일로 덮어쓰고) Start 하세요.

## Configuration

Add-on configuration:

```yaml
RS485:
  type: Socket
Socket:
  server: 192.168.x.x
  port: 8899
Serial:
  port1: /dev/ttyUSB0
MQTT:
  anonymous: false
  server: 192.168.x.x
  username: id
  password: pw
Wallpad:
  thermostat: true
  fan: true
Advanced:
  INIT_TEMP: 22
  SCAN_INTERVAL: 300
  SCANNING_INTERVAL: 0.8
  DEFAULT_SPEED: medium
  LOGLEVEL: info
KOCOM_ROOM_THERMOSTAT:
  - livingroom
  - bedroom
  - room1
  - room2
```

### Option: `RS485` (required)
```yaml
type: Socket                    // Serial 혹은 Socket
```
### Option: `Socket`
```yaml
server: 192.168.x.x           // Socket 쓸 경우 RS485-이더넷/WiFi 변환기 IP주소
port: 8899                    // Socket 쓸 경우 PORT번호
```
### Option: `Serial`
```yaml
port1: /dev/ttyUSB0           // Serial 쓸 경우 장치경로. 재연결에 대비해 /dev/serial/by-id/... 경로 권장
```

### Option `MQTT` (required)
```yaml
anonymous: false             // MQTT 설정
server: 192.168.x.xx         // MQTT 서버
username: id                 // MQTT ID
password: pw                 // MQTT PW
```

### Option `Wallpad` (required)
```yaml
thermostat: true             // 난방
fan: true                    // 환기팬
```

### Option `Advanced` (required)
```yaml
INIT_TEMP: 22           // 보일러 초기값
SCAN_INTERVAL: 300      // 월패드의 상태값 조회 간격
SCANNING_INTERVAL: 0.8  // 상태값 조회 시 패킷전송 간격
DEFAULT_SPEED: medium   // 환풍기 초기속도 low, medium, high
LOGLEVEL: info          // debug, info, warn 중에 하나
```

### Option `KOCOM_ROOM_THERMOSTAT` (optional)
```yaml
KOCOM_ROOM_THERMOSTAT:
  - livingroom
  - bedroom
  - room1
```
난방 방 패킷 00, 01, 02 ... 순서와 방이름이 매칭되어야 합니다.
월패드에서 난방을 작동하며 방이름(livingroom, bedroom, room1 등)을 확인하여 본인의 상황에 맞게 바꾸세요.

### 환기팬 전용 패킷
이 설치환경의 환기장치는 코콤 표준 환기 패킷으로 동작하지 않아 `rs485.py`의 `FAN_RAW_PACKETS`에 정의된
전용 패킷(on: 중간 속도, off)을 보냅니다. 응답을 확인할 수 없으므로 명령마다 5회 반복 전송합니다.

## Support

Got questions?

You have several options to get them answered:

- The [Korea Wallpad Controller][github].
- The [Home Assistant 네이버카페][forum].

버그신고는 카페나 깃허브로 해주세요 [open an issue on our GitHub][issue].


[forum]: https://cafe.naver.com/koreassistant
[github]: https://github.com/HAKorea/addons
[issue]: https://github.com/zooil/wallpadRS485/issues
[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
[armhf-shield]: https://img.shields.io/badge/armhf-yes-green.svg
[armv7-shield]: https://img.shields.io/badge/armv7-yes-green.svg
[i386-shield]: https://img.shields.io/badge/i386-yes-green.svg

