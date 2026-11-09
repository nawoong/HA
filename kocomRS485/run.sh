#!/bin/sh

SHARE_DIR=/share/kocom
BUNDLED=/rs485.py                 # 애드온 이미지에 들어 있는 새 버전
TARGET=$SHARE_DIR/rs485.py        # 실제로 실행하는 파일 (사용자가 수정 가능)
ORIG=$SHARE_DIR/.rs485.py.orig    # 마지막으로 설치한 원본. TARGET과 같으면 사용자 수정 없음

install_bundled() {
	cp "$BUNDLED" "$TARGET"
	cp "$BUNDLED" "$ORIG"
}

mkdir -p "$SHARE_DIR"
if [ ! -f "$TARGET" ]; then
	install_bundled
	echo "[Info] rs485.py 설치"
elif cmp -s "$BUNDLED" "$TARGET"; then
	cp "$BUNDLED" "$ORIG"
elif [ -f "$ORIG" ] && cmp -s "$TARGET" "$ORIG"; then
	install_bundled
	echo "[Info] rs485.py 새 버전으로 교체"
elif [ ! -f "$ORIG" ]; then
	# 이전 run.sh로 설치된 파일: 수정 여부를 알 수 없으므로 백업 후 교체
	cp "$TARGET" "$TARGET.bak"
	install_bundled
	echo "[Info] rs485.py 새 버전으로 교체 (기존 파일은 rs485.py.bak 으로 백업)"
else
	echo "[Warn] share/kocom/rs485.py 가 수정되어 있어 새 버전으로 교체하지 않습니다."
	echo "[Warn] 새 버전을 쓰려면 애드온을 Stop 하고 share/kocom/rs485.py 를 지운 뒤 Start 하세요."
fi
/makeconf.sh

echo "[Info] Run Wallpad Controller"
cd $SHARE_DIR
python3 $SHARE_DIR/rs485.py

# for dev
while true; do echo "still live"; sleep 100; done
