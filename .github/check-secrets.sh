#!/usr/bin/env bash
#
# 仓库里不能有密钥。这条约束是这套部署的基础——QQ 凭据只在 AstrBot 的 WebUI 里，
# 模型 key 由桥在运行时从 provider 配置里取，所以代码库里没有任何需要保密的东西。
# 一旦有人顺手 `git add .` 提交了 .env，这条脚本会挡住。
#
# 本地也能跑：.github/check-secrets.sh
# 传文件名就只查那几个（用来测它自己抓不抓得到）。
set -euo pipefail

cd "$(dirname "$0")/.."

fail() { printf '✗ %s\n' "$*" >&2; exit 1; }

# 用 heredoc 是为了那一对引号：正则里有 ' 和 "，塞进 shell 变量容易被引号吃掉。
PATTERN=$(cat <<'PATTERN_EOF'
(sk-[A-Za-z0-9]{16,}|(app_?secret|secret|password|passwd|api_?key|token|jwt)[[:space:]]*[:=][[:space:]]*["']?[A-Za-z0-9_./+-]{24,})
PATTERN_EOF
)

if [ $# -gt 0 ]; then
  files=("$@")
else
  mapfile -d '' -t files < <(git ls-files -z)
fi

# ── 文件名：这些进了版本库就是事故 ──────────────────────────────────
BANNED=$(printf '%s\n' "${files[@]}" |
  grep -E '(^|/)(\.env|qq\.env|credentials\.json)$|\.(pem|key|p12|pfx|jks|keystore)$|(^|/)id_(rsa|dsa|ecdsa|ed25519)' || true)
if [ -n "$BANNED" ]; then
  printf '✗ 这些文件不该被跟踪：\n%s\n' "$BANNED" >&2
  exit 1
fi

# ── 内容：值长得像密钥 ──────────────────────────────────────────────
if hits=$(grep -I -n -E -i -e "$PATTERN" -- "${files[@]}" 2>/dev/null); then
  printf '✗ 下面这些行看着像真密钥，确认是占位符就把这行改写：\n%s\n' "$hits" >&2
  exit 1
fi

printf 'ok   %d 个文件，没有密钥形状的东西\n' "${#files[@]}"