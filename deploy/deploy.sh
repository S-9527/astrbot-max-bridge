#!/usr/bin/env bash
#
# 在目标机上跑：把这份仓库更新到指定 ref，然后让 compose 生效，最后探活。
#
# CI 的用法（脚本从 stdin 送进去，远端不依赖仓库里已有的这一份）：
#
#   ssh user@host "bash -s --dir ~/max-qq" < deploy/deploy.sh
#
# 手动用法：
#
#   deploy/deploy.sh --dry-run     # 只说要做什么，完全不碰 docker 和 git
#   deploy/deploy.sh               # 真部署
#   deploy/deploy.sh --pull        # 顺便拉新镜像（默认不拉，部署要可复现）
#
# 这个脚本不碰 state/ 和 .env：两者都在 .gitignore 里，QQ 凭据、模型 key、
# id 映射库只存在于目标机上，CI 从头到尾看不到它们。
set -euo pipefail

REF=main
DIR=${HOME}/max-qq
REPO_URL=https://github.com/S-9527/astrbot-max-bridge.git
MAX_REPO_URL=https://github.com/HCHogan/max.git
PULL_IMAGES=0
FORCE=0
DRY_RUN=0
WAIT_SECONDS=900

while [ $# -gt 0 ]; do
  case "$1" in
    --ref)      REF=${2:?--ref 需要一个值}; shift 2 ;;
    --dir)      DIR=${2:?--dir 需要一个值}; shift 2 ;;
    --repo-url) REPO_URL=${2:?--repo-url 需要一个值}; shift 2 ;;
    --pull)     PULL_IMAGES=1; shift ;;
    --force)    FORCE=1; shift ;;
    --dry-run)  DRY_RUN=1; shift ;;
    --wait)     WAIT_SECONDS=${2:?--wait 需要一个秒数}; shift 2 ;;
    -h|--help)
      sed -n '2,16p' "$0"
      exit 0 ;;
    *)
      printf '不认识的参数：%s\n' "$1" >&2
      exit 2 ;;
  esac
done

step() { printf '\n── %s\n' "$*"; }
fail() { printf '✗ %s\n' "$*" >&2; exit 1; }
note() { printf '   %s\n' "$*"; }

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '   将执行：'
    printf ' %q' "$@"
    printf '\n'
  else
    printf '   $'
    printf ' %q' "$@"
    printf '\n'
    "$@"
  fi
}

step "前置检查"
command -v docker >/dev/null 2>&1 || fail "目标机上没有 docker"
docker compose version >/dev/null 2>&1 || fail "docker compose 不可用（需要 Compose v2 插件）"
if [ ! -f "$DIR/.env" ]; then
  # 不自动生成：STORAGE_ENCRYPTION_KEY 之类的密钥一旦被换掉，OmniRoute 磁盘上
  # 加密的旧数据就解不开了。宁可停下来让人来。
  if [ "$DRY_RUN" = 1 ]; then
    note "! 目标机还没有 $DIR/.env，真部署前必须先建好（见 DEPLOY.md）"
  else
    fail "缺少 $DIR/.env —— 这个脚本故意不生成它：密钥被换掉会让 OmniRoute 解不开旧数据"
  fi
fi
note "部署目录：$DIR"
note "目标 ref：$REF"

step "更新仓库到 $REF"
mkdir -p "$DIR"
OLD_SHA=""
if [ -d "$DIR/.git" ]; then
  # 只看已跟踪文件的改动，也就是 reset --hard 真正会丢的那些。
  # 用 git status --porcelain 会把未跟踪文件也算进来，可那些文件 reset 根本不会
  # 动——陌生人丢在目录里的一个 log 会平白挡住部署。
  DIRTY=$(git -C "$DIR" diff --name-only HEAD)
  if [ -n "$DIRTY" ]; then
    printf '目标机上的仓库有本地改动：\n%s\n' "$DIRTY" >&2
    if [ "$FORCE" != 1 ]; then
      fail "这些改动会被 --hard 丢掉。确认要丢就加 --force；不确认就自己去改"
    fi
    note "--force：丢掉上面这些改动"
  fi
  OLD_SHA=$(git -C "$DIR" rev-parse HEAD 2>/dev/null || printf '')
  run git -C "$DIR" fetch --quiet origin "$REF"
  run git -C "$DIR" checkout --quiet "$REF"
  # state/ 和 .env 不在版本库里，所以 reset --hard 不会碰它们。
  run git -C "$DIR" reset --quiet --hard "origin/$REF"
else
  run git clone --quiet "$REPO_URL" "$DIR"
fi

if [ "$DRY_RUN" = 1 ]; then
  NEW_SHA='(dry-run)'
  PLUGIN_CHANGED=1
else
  NEW_SHA=$(git -C "$DIR" rev-parse HEAD)
  PLUGIN_CHANGED=0
  if [ -n "$OLD_SHA" ] && [ "$OLD_SHA" != "$NEW_SHA" ]; then
    note "从 ${OLD_SHA:0:12} 到 ${NEW_SHA:0:12}"
    git -C "$DIR" log --oneline "$OLD_SHA..$NEW_SHA" | sed 's/^/     /'
    CHANGED=$(git -C "$DIR" diff --name-only "$OLD_SHA" "$NEW_SHA")
    if printf '%s\n' "$CHANGED" | grep -qE '(^|/)(main|ids|llm_proxy|media_server|onebot|__init__)\.py$|(^|/)metadata\.yaml$'; then
      PLUGIN_CHANGED=1
    fi
  else
    note "代码没变（同一个 commit），只做确保运行和探活"
  fi
fi

step "对齐 Max 的源码"
# 读第一个非注释行。不用 head -n1：这个文件第一行现在是 sha，但保不准哪天有人在
# 上面加了注释，那就会拿注释去 git checkout，报错还很难看懂。
# 用 awk 一次读完，别用管道——head/grep -m 提前退出会给上游发 SIGPIPE，在 pipefail
# 下变成「读到了但是失败了」。
PIN=$(awk '!/^[[:space:]]*#/ && NF { gsub(/[[:space:]]/, ""); print; exit }' "$DIR/deploy/max.pin" 2>/dev/null || printf '')
if [ -z "$PIN" ]; then
  if [ "$DRY_RUN" = 1 ]; then
    PIN='(dry-run)'
  else
    fail "$DIR/deploy/max.pin 是空的或不存在：不知道该用哪个 Max"
  fi
fi
MAX_SRC="$DIR/deploy/max-src"
MAX_SHA=""
if [ -d "$MAX_SRC/.git" ]; then
  MAX_SHA=$(git -C "$MAX_SRC" rev-parse HEAD)
fi
RECREATE_BOT=0
if [ "$MAX_SHA" != "$PIN" ]; then
  if [ "$MAX_SHA" = "" ]; then
    run git clone --quiet "$MAX_REPO_URL" "$MAX_SRC"
  fi
  run git -C "$MAX_SRC" fetch --quiet origin
  run git -C "$MAX_SRC" checkout --quiet "$PIN"
  RECREATE_BOT=1
  note "Max 换到 ${PIN:0:12}，容器会重新 nix 构建（第一次可能要十分钟）"
else
  note "Max 已经在 $PIN，不重建"
fi

step "让 compose 生效"
# dry-run 指向一个还不存在的目录时不能 cd 进去。
if [ -d "$DIR/deploy" ]; then
  cd "$DIR/deploy"
else
  note "将进入 $DIR/deploy"
fi
if [ "$PULL_IMAGES" = 1 ]; then
  run docker compose pull
else
  note "不拉镜像（要拉加 --pull）：部署要只跟着 commit 走，不跟着 latest 走"
fi
if [ "$RECREATE_BOT" = 1 ]; then
  run docker compose up -d --force-recreate bot
fi
run docker compose up -d --remove-orphans --wait --wait-timeout "$WAIT_SECONDS"
if [ "$PLUGIN_CHANGED" = 1 ]; then
  note "桥的代码变了，重启 astrbot 让它重新导入插件（QQ 会断线几秒）"
  run docker compose restart astrbot
  run docker compose up -d --wait --wait-timeout "$WAIT_SECONDS"
else
  note "桥的代码没变，不重启 astrbot（省一次无谓的 QQ 断线）"
fi

step "探活"
if [ "$DRY_RUN" = 1 ]; then
  note "将执行：docker compose exec -T astrbot python3 - < deploy/probe.py"
  step "（dry-run，到此为止）"
  exit 0
fi
# 从 stdin 送脚本时 $0 没有意义，所以用 $DIR 下的绝对路径。探针只发一个没有路由
# 匹配的请求，不外发、不花额度。
docker compose exec -T astrbot python3 - < "$DIR/deploy/probe.py" ||
  fail "桥的探针没过：插件多半没加载起来。看 docker compose logs astrbot | tail -50"

step "结果"
printf '   commit：%s\n' "$NEW_SHA"
git -C "$DIR" log -1 --format='   %h %ad %s' --date=short
docker compose ps --format '   {{.Service}}\t{{.Status}}' || true

cat <<'TXT'

   探针只证明插件活着，不证明 QQ 消息进得来。要确认整条链路，给机器人发一条
   消息，然后看 Max 的库：

     docker compose exec -T db psql -U max -d max -c \
       "select delivery_id,status,attempt_count,last_error from message_deliveries
        order by delivery_id desc limit 3"

   回滚（换 commit 不换代码）：

     git -C DIR reset --hard <旧的 commit> && (cd DIR/deploy && docker compose up -d)
TXT