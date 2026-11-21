# 安装、配置与运维

本文按仓库中的 Quark-VOD 3.0.0 源码、`docker-compose.yml` 和当前 API 行为编写。默认 Compose 部署从本地源码构建镜像；配置中的 `quark-vod:3.0.0` 是本地镜像标签，不代表已经发布到公共镜像仓库。镜像 OCI version label 默认跟随该标签；发布构建应显式传入 `QUARK_VOD_IMAGE_VERSION` 与 `QUARK_VOD_IMAGE_REVISION`，分别记录包版本和源码提交。

## 部署前准备

- Docker Engine 与 Docker Compose v2；或者 Linux、Python 3.11+，用于原生安装。
- 从项目发布渠道取得源码包，解压后进入包含 `docker-compose.yml`、`Dockerfile` 和 `pyproject.toml` 的项目根目录。当前仓库没有配置可直接 `docker pull` 的镜像地址，也没有在本文中假设 PyPI 发布渠道。
- 搜索本身只需要已启用且网络可达的搜索源；新转存还需要有效的夸克 Cookie 和可从服务进程访问的 OpenList。已经保存并写入状态库的内容，播放时主要需要状态记录，以及服务进程和实际播放器均可访问的 OpenList；不需要再次搜索或提交夸克保存。空配置可以启动，但健康检查成功不代表搜索、转存或播放依赖已就绪。

搜索配置由操作者自行提供。Compose 已挂载 `examples/` 目录；`examples/search.json`
是本机私有运行文件，新检出默认不存在，公开源码包也不会包含它。首次需要启用搜索
源时，可复制禁用占位项，再按[搜索插件文档](plugins.md)修改：

```sh
test -e examples/search.json || cp examples/search.example.json examples/search.json
```

占位地址使用 `.invalid`，节点默认禁用，不会提供可用搜索结果。

## Docker Compose 安装

在项目根目录创建私有环境文件，并在首次启动前设置管理员令牌。以下命令用随机 256 位令牌替换 `.env` 中的空 `VOD_ADMIN_TOKEN`；令牌不会打印到终端：

```sh
(
set -eu
umask 077
test -e .env || cp .env.example .env
chmod 600 .env
QUARK_VOD_ADMIN_TOKEN="$(openssl rand -hex 32)" python3 - <<'PY'
import os
from pathlib import Path
import tempfile

path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines()
key = "VOD_ADMIN_TOKEN="
matches = [line for line in lines if line.startswith(key)]
if len(matches) != 1:
    raise SystemExit(".env 中缺少 VOD_ADMIN_TOKEN 项")
if matches[0][len(key):].strip():
    raise SystemExit(".env 已有 VOD_ADMIN_TOKEN，未覆盖现有令牌")
lines = [key + os.environ["QUARK_VOD_ADMIN_TOKEN"] if line.startswith(key) else line for line in lines]
descriptor, temporary = tempfile.mkstemp(prefix=".env.", dir=".")
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write("\n".join(lines) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
except BaseException:
    os.unlink(temporary)
    raise
PY
)
```

编辑 `.env`，至少按你的环境设置 `OPENLIST_API`、`OPENLIST_PUBLIC`、`PENDING_BASE_URL`、`OPENLIST_ADMIN_PASS` 和 `QUARK_COOKIE`。不要把 Cookie、密码或管理员令牌提交到版本库。`.env` 已被 Git 忽略；仍应保留 `chmod 600`。

三个地址的用途不同：

- `OPENLIST_API` 是 Quark-VOD 服务进程访问 OpenList API 的地址。Compose 示例使用 `host.docker.internal`，只适用于 Docker 容器能够通过该名称访问宿主机服务的场景。
- `OPENLIST_PUBLIC` 必须同时是服务进程用于探测直链、实际播放器能够打开的 OpenList 地址；不能只在 Docker 宿主机或容器内可达。
- `PENDING_BASE_URL` 是播放器访问 Quark-VOD `/play` 等路径时使用的基础地址。远端电视或手机不能使用指向自身的 `localhost`。

检查 Compose 配置并启动：

```sh
docker compose config -q
docker compose up -d --build
docker compose ps
curl --fail --show-error http://127.0.0.1:8099/health
```

默认只将宿主机的 `127.0.0.1:8099` 发布到容器的 8099 端口。容器内部监听 `0.0.0.0` 不等于宿主机所有网络接口都已开放。Compose 将状态保存在持久卷中，容器根文件系统只读、以非 root 用户运行，内存限制为 256 MiB、进程数限制为 128。以下本机 curl 示例使用默认宿主机端口 8099；修改 `VOD_PORT` 后请相应调整。卷的真实名称可能带 Compose 项目前缀；不要假定它在 Docker 中的名字一定是裸 `quark_vod_data`。

打开 `http://127.0.0.1:8099/admin`，输入刚设置的管理员令牌。也可以在 Bash 中交互输入令牌，并用 API 检查鉴权：

```sh
read -rsp 'VOD_ADMIN_TOKEN: ' VOD_ADMIN_TOKEN; printf '\n'
curl --fail --show-error --silent \
  --config <(printf 'header = "Authorization: Bearer %s"\n' "$VOD_ADMIN_TOKEN") \
  http://127.0.0.1:8099/api/admin/plugins
unset VOD_ADMIN_TOKEN
```

`/health` 是公开的存活检查，不检查夸克 Cookie、OpenList、搜索源或播放能力。`/api/admin/*`、`/stats`、`/capability` 和 `/preheat` 要求 Bearer 令牌。管理页面、FastAPI `/docs` 和 `/openapi.json` 默认可公开读取；管理页面的令牌只在页面内存中使用，刷新后需要重新输入。

## 让局域网设备播放

默认端口仅绑定宿主机回环地址，只有宿主机自身能连接。要让电视或手机从局域网连接，可在 `.env` 中将 `VOD_PUBLISH_ADDRESS` 设为宿主机的局域网 IP，并将 `PENDING_BASE_URL`、`OPENLIST_PUBLIC` 改成播放设备能访问的地址。例如宿主机 IP 为 `192.168.1.10` 时：

```dotenv
VOD_PUBLISH_ADDRESS=192.168.1.10
VOD_PORT=8099
PENDING_BASE_URL=http://192.168.1.10:8099
OPENLIST_PUBLIC=http://192.168.1.10:5244
```

请替换成自己的地址，并确认 OpenList 的端口同时对服务进程和播放设备可达。修改后执行 `docker compose up -d` 使端口和环境变量生效。若通过反向代理或 VPN 提供访问，应确保服务进程和播放器都能访问配置的两个公共地址，并由网络边界限制访问范围。

播放接口 `/play`、`/pending`、`/media/{id}` 和 `/clean` 不要求管理员 Bearer；管理令牌不是媒体用户认证系统。不要把服务端口直接暴露到公网。远程访问应放在适合播放器的 TLS、VPN 或访问控制网关之后。

## 配置文件与凭据

Compose 的 `.env` 同时用于 Compose 插值和 `env_file` 注入容器；它不是应用自动发现的配置文件。Compose 固定容器内 `VOD_PORT=8099`、`VOD_BASE_DIR=/data` 和搜索配置 `/config/search.json`；`.env` 中的 `VOD_PORT` 只控制宿主机发布端口。

原生 Python 进程只有显式设置 `VOD_ENV_FILE=/绝对路径/文件` 才会读取环境文件。它不会自动搜索当前目录或 HOME 下的 `.env`。不要原样沿用 `.env.example` 的 Docker 地址：例如原生 Linux 通常不能解析 Compose 提供的 `host.docker.internal`，应改为 OpenList 的真实可达地址。进程环境中的普通配置值优先于指定环境文件中的值；显式设置为空字符串也会覆盖文件值。

常用设置如下，完整默认值及可选集成开关以 [`app/quark_vod/config.py`](../app/quark_vod/config.py) 为准：

| 设置 | 用途 |
|---|---|
| `VOD_BASE_DIR`、`VOD_DATA_DIR`、`VOD_LOG_DIR`、`VOD_STATE_DB` | 原生部署的数据、日志和 SQLite 状态位置；Compose 默认将状态置于 `/data` 卷 |
| `VOD_HOST`、`VOD_PORT` | 原生服务监听地址和端口；Compose 覆盖容器监听配置 |
| `VOD_ADMIN_TOKEN` | 管理 API 的 Bearer 令牌；不要留空后再开放管理 API |
| `VOD_SEARCH_CONFIG`、`VOD_SEARCH_PLUGINS` | 搜索 JSON 文件及显式允许的 Python provider entry point |
| `OPENLIST_API`、`OPENLIST_PUBLIC`、`OPENLIST_ADMIN_USER`、`OPENLIST_ADMIN_PASS` | OpenList 服务地址、播放器地址及访问凭据 |
| `QUARK_COOKIE`、`QUARK_MOUNT`、`QUARK_PLAY_MOUNT` | 夸克授权及写入、播放挂载路径 |
| `PENDING_BASE_URL` | 播放器访问 Quark-VOD 的基础 URL |
| `VOD_JELLYFIN_ENABLED`、`JELLYFIN2_API_BASE`、`JELLYFIN2_KEY`、`JELLYFIN2_DB` | 可选 Jellyfin 集成；默认关闭 |
| `VOD_MAINTENANCE_ENABLED`、`VOD_STARTUP_SCAN`、`VOD_STARTUP_WRITE_PROBE`、`SELFHEAL_ENABLED` | 维护、启动扫描、启动写探测和后台自愈；默认关闭 |
| `AI_PARSE_ENABLED`、`AI_PARSE_BASE_URL`、`AI_PARSE_MODEL`、`OPENAI_API_KEY` | 可选 AI 弱名称辅助；默认关闭 |

支持对应 `_FILE` 文件来源的凭据有 `OPENLIST_ADMIN_USER`、`OPENLIST_ADMIN_PASS`、`VOD_ADMIN_TOKEN`、`JELLYFIN2_KEY`、`QUARK_COOKIE` 和 `OPENAI_API_KEY`。AI 解析 token 使用单独的 `AI_PARSE_TOKEN_FILE`。凭据文件应以只读方式挂载。若同一凭据的普通变量和 `_FILE` 同时存在，普通变量优先，即使它是空值；使用文件方式时，应从 `.env`/`env_file` 中移除该普通变量。`VOD_ADMIN_TOKEN_FILE` 仅设置路径还不够，必须同时挂载文件。

Compose 示例的 `QUARK_COOKIE`、`OPENLIST_ADMIN_PASS` 和 `VOD_ADMIN_TOKEN` 是空项。启用 `_FILE` 前移除相应空项，否则空值会覆盖文件。管理台修改的夸克 Cookie 会写入持久数据目录中的 `quark_cookie.txt`，并持续覆盖环境中的 Cookie，直到通过管理 API 清除该持久值。

Docker 可将管理员令牌单独放在宿主机文件中。Compose 会把文件以 bind mount
提供给固定的容器 UID/GID `10001:10001`；宿主机文件不能使用只有宿主用户可读的
`0600`，否则容器进程通常无法读取。下面用 `0700` 的父目录限制宿主机其他普通用户
访问，再让挂载文件可读；Docker 守护进程和容器内的服务进程仍能读取该文件：

```sh
install -d -m 700 "$HOME/.config/quark-vod"
token_path="$HOME/.config/quark-vod/admin-token"
token_tmp="$(mktemp "${token_path}.tmp.XXXXXX")"
trap 'rm -f "$token_tmp"' EXIT
openssl rand -hex 32 > "$token_tmp"
chmod 444 "$token_tmp"
mv -f "$token_tmp" "$token_path"
trap - EXIT
```

如果宿主机有更严格的 UID/GID 管理，也可以把文件属主/属组设为容器的
`10001:10001` 并使用 `0400` 或 `0440`；不要在未验证容器可读性的情况下直接使用
`0600`。宿主机的 Docker daemon/root 仍可能读取 bind mount，这是本地容器部署的信任边界。

在项目根目录创建 `compose.secrets.yml`，并将宿主机路径替换成实际路径：

```yaml
services:
  quark-vod:
    environment:
      VOD_ADMIN_TOKEN_FILE: /run/secrets/vod_admin_token
    volumes:
      - /home/USER/.config/quark-vod/admin-token:/run/secrets/vod_admin_token:ro
```

删除 `.env` 中的 `VOD_ADMIN_TOKEN=` 行，再用 `-f docker-compose.yml -f compose.secrets.yml` 作为 Compose 文件参数运行后续的 `config`、`up`、`exec` 等命令。只设置容器路径但不挂载宿主机文件会导致管理认证不可用。

## 原生 Python 安装

在 Linux 上从项目源码根目录安装。创建原生专用配置，生成独立令牌文件并引用它；同时把 OpenList 地址改为本机或服务进程实际可达的地址：

```sh
(
set -eu
test -e .env.native || cp .env.example .env.native
chmod 600 .env.native
install -d -m 700 "$HOME/.config/quark-vod"
token_path="$HOME/.config/quark-vod/admin-token"
if test -e "$token_path"; then
  printf '令牌文件已存在，未覆盖：%s\n' "$token_path" >&2
  exit 1
fi
token_tmp="$(mktemp "${token_path}.tmp.XXXXXX")"
trap 'rm -f "$token_tmp"' EXIT
openssl rand -hex 32 > "$token_tmp"
chmod 600 "$token_tmp"
if ! ln "$token_tmp" "$token_path"; then
  printf '无法创建令牌文件，目标可能已存在：%s\n' "$token_path" >&2
  exit 1
fi
rm -f "$token_tmp"
trap - EXIT
sed -i '/^VOD_ADMIN_TOKEN=/d; /^VOD_ADMIN_TOKEN_FILE=/d' .env.native
printf 'VOD_ADMIN_TOKEN_FILE=%s\n' "$HOME/.config/quark-vod/admin-token" >> .env.native
python3 -m venv .venv
.venv/bin/python -m pip install .
)
```

编辑 `.env.native`，设置实际服务地址；令牌文件路径已写入配置，不要再添加普通的 `VOD_ADMIN_TOKEN` 值。需要搜索时，再复制并配置 `examples/search.json`。然后运行：

```sh
VOD_ENV_FILE="$PWD/.env.native" \
VOD_SEARCH_CONFIG="$PWD/examples/search.json" \
  .venv/bin/quark-vod --host 127.0.0.1 --port 8099
```

原生默认仅绑定 `127.0.0.1`。如要让其他设备访问，必须显式选择可达的监听地址、配置对应的 `PENDING_BASE_URL` 和 `OPENLIST_PUBLIC`，并设置防火墙或受控访问层。单实例只使用一个进程和一个 worker；SQLite 状态、转存协调与缓存容量均按单进程设计。Python 包从本地源码安装，本文不假设有可直接 `pip install quark-vod` 的外部包索引。

## 搜索源配置

Compose 将整个 `examples/` 目录以只读方式挂载到容器 `/config`，服务读取 `/config/search.json`，对应宿主机的 `examples/search.json`。修改宿主文件即可更新搜索源；有效 JSON 在后续搜索请求时热加载，错误配置保留上一份有效快照。自定义 Compose 部署也应挂载目录，而不是只挂单个 JSON 文件，以免宿主机原子替换文件后容器仍引用旧 inode。安装或更新 Python provider 代码需要重启服务；JSON 节点配置无需安装代码。节点字段和错误语义见[搜索插件文档](plugins.md)。

## Jellyfin 与独立播放模式

关闭 Jellyfin 时，成功解析后直接返回已验证的播放 URL，不创建 STRM 文件。搜索本身只需要已启用的搜索源；新转存还需要夸克授权及 OpenList；已有保存记录的播放只需状态记录，以及服务进程和播放器均可访问的 OpenList。

启用 Jellyfin 是可选集成。请求的影片或剧集必须已有稳定 STRM 映射；缺失映射是集成配置问题，不代表夸克云端没有资源。Jellyfin 的密钥、库路径和保护数据库必须按本机部署单独配置。启用维护或删除操作前，先阅读[架构说明](architecture.md)和对应集成契约。

## 升级、备份与回滚

### 从旧的宿主耦合部署迁移

3.0.0 将公共 Compose 与宿主集成分开。旧部署可能使用 host 网络、宿主目录和源码挂载；新版公共 Compose 使用独立的数据卷，默认只发布回环端口。直接替换 Compose 后启动，可能得到一个全新的空状态库；这不表示旧数据已经迁移。数据库 schema 兼容不等于部署配置兼容。

替换旧 checkout 前，保留旧 Compose、覆盖文件、项目名和实际挂载信息。使用旧镜像及旧 Compose 完成下方备份流程，把 `state_db` 改为旧容器实际使用的状态库路径。确认已备份状态库、STRM/NFO、凭据和搜索配置后，再选择继续使用宿主适配，或将已校验的快照迁入独立部署。

沿用旧数据时，通过自己的 Compose 覆盖文件显式保留数据挂载和容器内路径，并设置 `VOD_STATE_DB`、`VOD_CINEMA2_DIR` 及所需集成。`strm_map` 中的既有路径必须仍能从新容器访问；只复制 SQLite 文件不能完成媒体库迁移。核对非 root 用户对各持久目录的访问权限，不要通过递归修改整个媒体库属主来解决权限错误。保留旧部署和备份，验收后再决定是否移除。

新版本只在显式指定 `VOD_ENV_FILE` 时读取环境文件；搜索源、OpenList 凭据来源和可选集成也必须明确配置。Jellyfin、AI、自愈和破坏性维护默认关闭，不能依赖旧版的隐式 HOME 路径、站点或开关。

### 备份与回滚流程

升级前安排维护窗口，确认没有正在进行的远端保存或播放，再暂停入口和共用状态库的宿主作业。停止所有旧版服务进程、容器和其他写者，并确认它们已退出后才启动新版；升级不支持新旧版本重叠运行。SQLite 启动锁只串行化迁移过程，不能阻止旧进程在迁移完成后继续写。最近记录只能辅助判断。保留旧版 Compose 文件、项目名和启动参数、`.env`、私有搜索配置、凭据文件及集成部署的 STRM/NFO；备份应限制访问。

以下 Bash 示例在旧版项目目录执行。若部署使用自定义项目名、环境文件或覆盖文件，请将这些参数加入 `compose` 数组。自定义数据目录时，先确认实际数据卷及容器内状态库路径，再调整 `state_db`。脚本错误即停止，服务可能已停止；备份未经校验不能继续升级。

```bash
(
set -euo pipefail
umask 077
compose=(docker compose -f docker-compose.yml)
state_db=/data/data/quark_plays.db
container=$("${compose[@]}" ps --all --quiet quark-vod)
[[ -n "$container" && "$container" != *$'\n'* ]] || {
  echo '预期 quark-vod 恰好对应一个容器，已停止操作。' >&2
  exit 1
}
image_id=$(docker inspect --type container --format '{{.Image}}' "$container")
backup_dir=$(mktemp -d "$PWD/quark-vod-backup.XXXXXXXX")
printf '备份目录：%s\n' "$backup_dir"
rollback_image="quark-vod:rollback-${backup_dir##*.}"
docker image tag "$image_id" "$rollback_image"
docker image save -o "$backup_dir/image.tar" "$rollback_image"
printf 'services:\n  quark-vod:\n    image: %s\n' "$rollback_image" > "$backup_dir/rollback.yml"

"${compose[@]}" stop quark-vod
temporary=$(mktemp "$backup_dir/.state.XXXXXXXX")
trap 'rm -f -- "$temporary"' EXIT
"${compose[@]}" -f "$backup_dir/rollback.yml" run --rm --no-deps -T \
  --pull never --entrypoint python quark-vod - "$state_db" > "$temporary" <<'PY'
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

with tempfile.TemporaryDirectory() as directory:
    copy = Path(directory) / "state.db"
    with closing(sqlite3.connect(Path(sys.argv[1]).as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(copy)) as destination:
            source.backup(destination)
            if destination.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                raise SystemExit("无法生成独立的 SQLite 快照")
    with copy.open("rb") as stream:
        shutil.copyfileobj(stream, sys.stdout.buffer)
PY
test -s "$temporary"
python3 - "$temporary" <<'PY'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

with closing(sqlite3.connect(Path(sys.argv[1]).as_uri() + "?mode=ro", uri=True)) as db:
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise SystemExit("SQLite 备份校验失败")
    print("备份校验通过，schema =", db.execute("PRAGMA user_version").fetchone()[0])
PY
ln -- "$temporary" "$backup_dir/state.db"
)
```

SQLite backup API 将已提交的 WAL 内容纳入快照。备份容器在临时目录生成快照；数据库若接近 Compose 的 `/tmp` 容量上限，须为备份命令提供容量足够的专用临时空间。成功后保留完整备份目录，其中有旧镜像、回滚覆盖文件和通过校验的状态库。

当前代码支持把 `PRAGMA user_version=0` 的既有 schema 迁移到版本 1。升级时服务先在事务内执行 SQLite `quick_check`，再迁移 schema 并检查必需表和列；任何完整性或迁移检查失败都会回滚并阻止服务继续启动。已经是当前版本的数据库不会在每次日常启动时重复扫描。版本高于当前程序支持值的数据库会被拒绝，程序不会自动降级，也不会自动创建备份。若启动报 schema 或完整性错误，保留数据库和日志，先确认运行的程序版本及备份；不要手工改 `user_version` 让旧程序绕过检查。恢复数据库前先停止服务，并确认接受丢弃备份之后写入的状态。

换成新版源码和匹配配置后，沿用同一 Compose 项目名和原数据卷。使用同样的项目参数及需要的覆盖文件，依次执行：

```sh
docker compose config -q
docker compose build quark-vod
docker compose up -d --no-build --pull never --force-recreate quark-vod
```

核对 `/health` 返回的版本并完成下方验收，再恢复定时任务和入口。只有确认旧程序兼容当前状态库与配置时，才能只回滚镜像。停掉服务和共享写者后，切回旧版配置，确认项目名、卷和路径正确。在其他覆盖文件之后追加备份目录的 `rollback.yml`，独立选用旧镜像。默认 Compose 示例：

```sh
backup_dir=/absolute/path/to/quark-vod-backup.XXXXXXXX
docker image load -i "$backup_dir/image.tar" &&
  docker compose -f docker-compose.yml -f "$backup_dir/rollback.yml" config -q &&
  docker compose -f docker-compose.yml -f "$backup_dir/rollback.yml" \
    up -d --no-build --pull never --force-recreate quark-vod
```

状态库不兼容时，先停掉全部写者并关闭其他数据库连接。保留升级后的配置和状态库，把主库及存在的 `-wal`、`-shm`、`-journal` 一起移离原路径。只有接受备份后新增的状态记录丢失时，才把校验过的 `state.db` 恢复到原路径，保持卷内属主和权限。恢复的主库不能与之前的 WAL/SHM 混用。再次验证完整性后，用与备份匹配的镜像和配置启动。升级与回滚均应保留数据卷。

## 验收顺序

按层验证，不要把单个健康检查当成可播证明：

1. `/health` 返回成功：仅证明 HTTP 服务进程可响应。
2. `/api/admin/plugins` 可用：证明管理员令牌配置和认证有效；搜索状态仍需确认已配置并启用预期节点。
3. 只有显式 `POST /api/admin/capability/probe` 才执行能力探测；它会联系夸克和 OpenList，并可能执行唯一测试写入/删除。仅在已理解副作用并准备好时触发，不要把普通 GET 状态查询当成探测。
4. 使用允许的单个作品验证搜索、保存、OpenList 读取及播放器实际起播与 seek。保存完成、字节读取成功和终端播放是不同验收结果。

搜索节点配置和热重载语义见[搜索插件文档](plugins.md)。安全边界见[安全说明](../SECURITY.md)。
