#!/bin/sh
# BeauClaw release installer. The final main call makes this safe to pipe to sh.

die() { printf 'BeauClaw: %s\n' "$*" >&2; exit 1; }
download() { curl -fLsS --retry 3 --connect-timeout 15 "$1" -o "$2"; }
on_path() { case ":${PATH:-}:" in *":$1:"*) return 0 ;; *) return 1 ;; esac; }
privileged() {
    if [ "$(id -u)" = 0 ]; then
        "$@" </dev/null
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@" </dev/null
    else
        die "安装 tmux 需要管理员权限；请先安装 tmux 后重试。"
    fi
}
ensure_tmux() {
    command -v tmux >/dev/null 2>&1 && return
    printf '正在安装 tmux…\n'
    if command -v brew >/dev/null 2>&1; then brew install tmux </dev/null
    elif command -v apt-get >/dev/null 2>&1; then
        privileged apt-get update
        privileged apt-get install -y tmux
    elif command -v dnf >/dev/null 2>&1; then privileged dnf install -y tmux
    elif command -v yum >/dev/null 2>&1; then privileged yum install -y tmux
    elif command -v apk >/dev/null 2>&1; then privileged apk add tmux
    elif command -v pacman >/dev/null 2>&1; then privileged pacman -S --needed --noconfirm tmux
    else die "未找到可用的包管理器，请先安装 tmux。macOS 可运行 brew install tmux。"
    fi
    command -v tmux >/dev/null 2>&1 || die "tmux 安装失败。"
}

main() {
    set -eu
    [ "$#" = 0 ] || die "使用环境变量 BEAUCLAW_VERSION 指定版本，不接受命令行参数。"
    case "$(uname -s)" in Linux|Darwin) ;; *) die "目前支持 Linux 和 macOS。" ;; esac
    command -v curl >/dev/null 2>&1 || die "请先安装 curl。"
    command -v tar >/dev/null 2>&1 || die "请先安装 tar。"
    beau_version=${BEAUCLAW_VERSION:-0.2.0}
    case "$beau_version" in v*) beau_version=${beau_version#v} ;; esac
    case "$beau_version" in ''|*[!0-9.]*|.*|*.) die "无效版本号。" ;; esac
    beau_release_base=${BEAUCLAW_RELEASE_BASE_URL:-https://github.com/winbeau/tools-claw/releases}
    beau_assets="${beau_release_base%/}/download/v$beau_version"
    beau_app_root=${BEAUCLAW_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/beauclaw/app}
    beau_bin_dir=${BEAUCLAW_BIN_DIR:-$HOME/.local/bin}
    if [ -z "${BEAUCLAW_BIN_DIR:-}" ] && ! on_path "$beau_bin_dir"; then
        if on_path /usr/local/bin && [ -w /usr/local/bin ]; then beau_bin_dir=/usr/local/bin; fi
    fi
    mkdir -p "$beau_app_root" "$beau_bin_dir"
    beau_app_root=$(cd "$beau_app_root" && pwd -P)
    beau_bin_dir=$(cd "$beau_bin_dir" && pwd -P)
    beau_tmp=$(mktemp -d "${TMPDIR:-/tmp}/beauclaw-install.XXXXXX")
    trap 'rm -rf "$beau_tmp"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    printf '下载 BeauClaw v%s…\n' "$beau_version"
    download "$beau_assets/beauclaw.tar.gz" "$beau_tmp/beauclaw.tar.gz"
    download "$beau_assets/SHA256SUMS" "$beau_tmp/SHA256SUMS"
    beau_expected=$(awk '$2 == "beauclaw.tar.gz" {print $1}' "$beau_tmp/SHA256SUMS")
    [ "${#beau_expected}" = 64 ] || die "校验文件格式错误。"
    case "$beau_expected" in *[!0-9a-f]*) die "校验文件包含无效 SHA-256。" ;; esac
    if command -v sha256sum >/dev/null 2>&1; then
        beau_actual=$(sha256sum "$beau_tmp/beauclaw.tar.gz" | awk '{print $1}')
    elif command -v shasum >/dev/null 2>&1; then
        beau_actual=$(shasum -a 256 "$beau_tmp/beauclaw.tar.gz" | awk '{print $1}')
    else die "请先安装 sha256sum 或 shasum。"
    fi
    [ "$beau_actual" = "$beau_expected" ] || die "SHA-256 校验失败，安装已停止。"
    ensure_tmux
    if command -v uv >/dev/null 2>&1; then beau_uv=$(command -v uv)
    elif [ -x "$HOME/.local/bin/uv" ]; then beau_uv="$HOME/.local/bin/uv"
    elif [ -x "$beau_app_root/uv/uv" ]; then beau_uv="$beau_app_root/uv/uv"
    else
        printf '正在安装 uv…\n'
        download https://astral.sh/uv/install.sh "$beau_tmp/uv-install.sh"
        UV_UNMANAGED_INSTALL="$beau_app_root/uv" sh "$beau_tmp/uv-install.sh" </dev/null
        beau_uv="$beau_app_root/uv/uv"
    fi
    beau_release_dir="$beau_app_root/v$beau_version"
    if [ -f "$beau_release_dir/.archive.sha256" ]; then
        [ "$(cat "$beau_release_dir/.archive.sha256")" = "$beau_expected" ] || die "已安装的同名版本校验不同，拒绝覆盖。"
    else
        tar -xzf "$beau_tmp/beauclaw.tar.gz" -C "$beau_tmp"
        [ -f "$beau_tmp/beauclaw-release/pyproject.toml" ] || die "安装包结构错误。"
        mkdir -p "$beau_release_dir"
        cp -R "$beau_tmp/beauclaw-release/." "$beau_release_dir/"
        printf '%s\n' "$beau_expected" > "$beau_release_dir/.archive.sha256"
    fi
    printf '通过 uv 安装 Python 3.12 环境与锁定依赖…\n'
    UV_PROJECT_ENVIRONMENT="$beau_release_dir/.venv" "$beau_uv" sync \
        --project "$beau_release_dir" --python 3.12 --frozen --extra browser --no-dev </dev/null
    "$beau_release_dir/.venv/bin/python" -c 'import sys; assert sys.version_info[:2] == (3, 12)'
    [ ! -d "$beau_bin_dir/beauclaw" ] || die "命令目标是目录，拒绝覆盖。"
    ln -s "$beau_release_dir/.venv/bin/beauclaw" "$beau_bin_dir/.beauclaw-install-$$"
    mv -f "$beau_bin_dir/.beauclaw-install-$$" "$beau_bin_dir/beauclaw"
    "$beau_bin_dir/beauclaw" --version
    printf '\n安装完成：%s/beauclaw\n' "$beau_bin_dir"
    if ! on_path "$beau_bin_dir"; then
        if [ "$beau_bin_dir" = "$HOME/.local/bin" ]; then
            case "${SHELL:-}" in */zsh) beau_profile="$HOME/.zshrc" ;; */bash) beau_profile="$HOME/.bashrc" ;; *) beau_profile="$HOME/.profile" ;; esac
            if ! grep -Fq '# BeauClaw executable path' "$beau_profile" 2>/dev/null; then
                printf '\n# BeauClaw executable path\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$beau_profile"
            fi
            printf '当前终端先运行：export PATH="$HOME/.local/bin:$PATH"\n'
        else
            printf '请将 %s 加入 PATH，或使用上述命令的绝对路径。\n' "$beau_bin_dir"
        fi
    fi
    printf '\n首次使用：\n  beauclaw config set\n  beauclaw notice add 通知邮箱\n  beauclaw login --browser\n  beauclaw start\n\n管理：beauclaw status / beauclaw stop\n'
    printf '升级：再次运行安装命令，再执行 beauclaw stop && beauclaw start。\n'
}

main "$@"
