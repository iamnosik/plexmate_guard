# Plexmate Guard

FlaskFarm용 Plexmate Guard 플러그인입니다. Plex 메타데이터 큐, SJVA agent 대기, Plex Scanner, Plexmate 작업 수와 NAS 부하를 한 화면에서 관찰하고, 사용자가 Plexmate 신규 스캔을 `2 → 1 → 0`으로 안전하게 제어할 수 있게 합니다.

## 0.2.0 안전 범위

- Plex 또는 FF를 자동 재시작하지 않습니다.
- 기존 FF Command watchdog을 수정하거나 켜지 않습니다.
- `0`은 진행 중인 스캔을 종료하지 않으며 이후 신규 Plexmate 스캔 시작만 막습니다.
- 대시보드의 **Plex 수동 재시작**은 Plexmate 제한이 실제 `0`이고, 네이티브 또는 명시한 Docker Plex 대상이 확인된 경우에만 활성화됩니다.
- 수동 재시작은 두 번의 사용자 확인 뒤 한 번만 요청하며, 실패·시간 초과 시 자동 재시도하지 않습니다.
- Plex 데이터베이스 마이그레이션 응답(503)은 수동 재시작도 차단합니다.
- 기본으로 켜진 **상세 운영 로그**는 조회·관찰마다 HTTP 응답시간, 큐 변화, SJVA 최대 대기, 최근 timeout, Plexmate 작업 수·제한, 설치 방식과 NAS 부하를 기록합니다. 토큰·원본 Plex 로그·개별 파일 경로는 기록하지 않습니다.
- 개별 metadata 갱신은 상태 조회와 분리된 30초 제한 시간을 사용합니다. 그 안에 응답하지 않으면 Plex가 계속 처리 중일 수 있으므로 해당 후보의 즉시 중복 요청을 막고 확인을 안내합니다.

## 설치

FF의 **시스템 → 플러그인 → 로딩 플러그인**에서 아래 URL을 설치합니다.

```
https://github.com/iamnosik/plexmate_guard
```

설치 뒤 **Plexmate Guard → 설정**에서 로그 경로와 관찰 기준을 확인하고, 먼저 `Guard 사용`을 켠 뒤 관찰 모드로 사용하세요.

## 롤백

Guard를 끄고 대시보드에서 `기본값 복원`을 누르면 Plexmate 실행 제한을 저장된 기본값으로 되돌립니다. Guard DB와 기존 Plexmate/Plex 데이터는 삭제하지 않습니다.
