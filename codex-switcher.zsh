# 使用：source /apps/yly/codex-account-switcher/codex-switcher.zsh

if [[ -n "${ZSH_VERSION:-}" ]]; then
  _CXS_DIR="${${(%):-%N}:A:h}"
else
  _CXS_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
fi
_CXS_BIN="${_CXS_DIR}/codex-switcher.py"

cxs() {
  case "${1:-status}" in
    use|switch)
      if [[ -z "${2:-}" ]]; then
        echo "用法：cxs use <账号编号>（正整数，例如 1、2、3）" >&2
        return 2
      fi
      local _cxs_env
      _cxs_env="$("$_CXS_BIN" env "$2")" || return
      eval "${_cxs_env}"
      "$_CXS_BIN" use "$2" >/dev/null || return
      echo "已切换到账号 $2；当前 shell 中的 codex 将使用该账号。"
      ;;
    resume)
      if [[ -n "${2:-}" && "${2:-}" != -* ]]; then
        cxs use "$2" || return
      fi
      "$_CXS_BIN" "$@"
      ;;
    run|exec|codex)
      shift
      "$_CXS_BIN" run "$@"
      ;;
    *)
      "$_CXS_BIN" "$@"
      ;;
  esac
}

# source 后，直接输入 codex 也会跟随 cxs use 的账号。
codex() { "$_CXS_BIN" run "$@"; }
export CODEX_SWITCHER_BIN="$_CXS_BIN"
