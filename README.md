# dow2-ladder — сайт, ладдер и сервисы DoW2 Elite

Всё, что крутится на rts.fintebtc.de (и зеркалах dow2 / dow2ru) вокруг автоматча и сайта, плюс наш игровой сервер в Москве.

| Папка | Что это | Куда ставится | Сервис |
|---|---|---|---|
| `ladder/` | очередь автоматча, подбор, история матчей, рейтинг, чат из лаунчера (`/ladder/`) | `/srv/ladder/app/` | `dow2-ladder` (порт 8217) |
| `stats/` | статистика игроков Relic, рейтинг для LobbyStats, приём логов из лаунчера (`/stats/`) | `/srv/relicstats/app/` | `relic-stats` (8216) |
| `replay/` | реплеи: загрузка, дубли, разбор, истории (`/replay/`) | `/srv/replayweb/app/`, воркер — `/usr/local/bin/` | `replay-web`, `replay-story` |
| `site/` | лендинг Elite (корень сайта и `/elite/`), Elite Lab, ассеты рас/карт | `/var/www/rts-site/elite/` (кроме `dl/` — его заливает CI лаунчера) | nginx |
| `gameserver/` | наш игровой сервер (`battle_relay.py`, порт 47100) и старый пересыльщик в Relic (`relay_proxy.py`) | Timeweb: `/srv/dow2battle/`, `/srv/dow2relay/` | `dow2-battle`, `dow2-relay` |
| `deploy/` | скрипт выкладки, юниты systemd, уведомление о логах | — | ставится руками |

Данные (базы, токены, ключи) в репозитории не лежат: они в `/srv/*/data/` на сервере.

## Как заливать

Ключ один на две машины: **`/root/.ssh/deploy_dow2-ladder`** (основной сервер и prod_gpu), deploy key репозитория с правом записи.

```bash
export GIT_SSH_COMMAND="ssh -i /root/.ssh/deploy_dow2-ladder -o IdentitiesOnly=yes"
git clone git@github.com:pivolan/dow2-ladder.git
# правки → коммит → push в main
git -c user.name=pivolan -c user.email=pivolan@users.noreply.github.com commit -am "что и зачем"
git push origin main
```

Рабочие копии: основной сервер — `/srv/dow2-web`, prod_gpu — `/mnt/dow2bench/dev/dow2-web`.
Автор коммитов — `pivolan <pivolan@users.noreply.github.com>`.

## Как выкладывается (пайплайн)

1. **GitHub Actions** (`.github/workflows/check.yml`) на каждый push и PR проверяет, что весь Python компилируется. Секреты не нужны.
2. **Сервер сам забирает main.** Таймер `dow2-web-deploy.timer` на основном сервере раз в 2 минуты запускает
   `deploy/deploy.sh`: `git fetch` по deploy-ключу → проверка → ставит только изменившиеся части → перезапускает только их
   → пишет в Telegram (тред dow_replay) «🚀 выложено: …» или «⛔ не компилируется».
3. **Бережно к игрокам:**
   - ладдер держит очередь в памяти — пока кто-то ищет или собирается в матч, перезапуск ждёт (до 10 минут);
   - игровой сервер в Москве перезапускается, только когда на нём нет ни одной игры (иначе ждёт следующего запуска).
4. Коммит, который не прошёл проверку, второй раз не выкладывается — нужен новый коммит.

Откатить = `git revert` + push (через 2 минуты на сервере).

## Ручная установка выкладки (один раз)

```bash
install -m 755 deploy/deploy.sh /usr/local/bin/dow2-web-deploy
cp deploy/systemd/dow2-web-deploy.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now dow2-web-deploy.timer
```
