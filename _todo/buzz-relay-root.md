# Buzz relay 띄우기 위한 root 작업

상태: 대기 (사용자가 root로 진행)

relay 설정은 `deploy/buzz-relay/`에 있다. automan은 docker와 Caddyfile을 다룰 수 없으므로 아래는 root로 한다.

## 1. automan에게 docker 권한 주기

```bash
usermod -aG docker automan
```

docker 그룹은 사실상 root 권한과 같다. automan으로 relay를 올리고 내리고 로그를 보려면 필요하다.
적용하려면 automan 세션을 새로 열어야 한다(Claude Code 재시작).

## 2. Caddy에 :8443 추가

`/etc/caddy/Caddyfile` 끝에 추가한다. 기존 블록은 그대로 둔다.

```
srv2036064.hstgr.cloud:8443 {
	encode zstd gzip
	reverse_proxy 127.0.0.1:3000
}
```

```bash
systemctl reload caddy
```

## 3. 방화벽에서 8443/tcp 열기

- `ufw status`가 active면 `ufw allow 8443/tcp`
- Hostinger hPanel → VPS → 방화벽을 쓰고 있다면 거기도 8443/tcp 허용 규칙을 넣는다.

## 끝나면

Claude에게 알린다. Claude가 `docker compose up -d`로 relay를 올리고, 봇 연결을 확인한 뒤 초대 링크를 만든다.
