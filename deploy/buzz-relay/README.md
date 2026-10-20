# Buzz relay (자체 운영)

Buzz 봇(`app/jobs/buzz_bot.py`)과 폰 Buzz 앱이 붙는 커뮤니티 relay. [block/buzz](https://github.com/block/buzz)의
`deploy/compose` 를 옮겨, relay 를 `127.0.0.1:3000` 에만 열고 호스트 Caddy 가 `:8443` 에서 TLS 를 맡는다.

```
폰 Buzz 앱 ──wss──▶ Caddy :8443 ──▶ relay 127.0.0.1:3000 (docker) ── postgres, redis, minio(silo)
auto_mobile 봇 ──https──┘
```

- 주소: `https://srv2036064.hstgr.cloud:8443` (RELAY_URL `wss://…:8443`). 기존 API 와 같은 호스트명이라 인증서를 같이 쓴다.
- 커뮤니티 owner 는 auto_mobile 봇의 pubkey 다. 봇이 초대 링크를 만들고, 사용자는 그 링크로 들어온다.
- 설정은 `.env`(git 제외, 600). `env.example` 을 복사해 비밀값을 채운다. relay 키·DB 비밀번호는 바꾸지 않는다.
- 이미지는 `BUZZ_IMAGE` 로 고정한다. 봇이 쓰는 buzz CLI(데스크톱 .deb)와 비슷한 시점의 main 빌드를 고른다.

## 운영

```bash
cd deploy/buzz-relay
docker compose up -d                 # 시작 (재부팅 후에는 restart: unless-stopped 로 자동 시작)
docker compose ps
docker compose logs -f relay
docker compose exec relay buzz-admin list-members
docker compose exec relay buzz-admin add-member --pubkey npub1... [--role admin]
```

초대 링크(7일, 1회용): `uv run python -m app.jobs.buzz_bot --invite` (저장소 루트에서). 폰 Buzz 앱에는 `app_url`(`buzz://join?relay=wss://…&code=…`)을 넣는다. `url`은 웹 안내 페이지다. 앱 첫 화면의 **Enter a community link** 에 넣는다. "Use pairing code" 칸은 데스크톱 앱의 키를 옮기는 용도라 초대 링크를 넣으면 `Invalid base64 data` 가 난다.

## 백업

데이터는 docker volume `buzz_buzz-postgres-data`, `buzz_buzz-minio-data`, `buzz_buzz-redis-data`, `buzz_buzz-git-data` 와 `.env` 다.
