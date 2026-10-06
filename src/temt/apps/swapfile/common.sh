# shellcheck shell=bash
# install と uninstall が読み込む、使うファイルのパスと、共通の手順。deploy と app remove が
# root で実行する

# swap に使うファイル
SWAPFILE=/swapfile
# 起動のたびに swap を使わせる fstab
FSTAB=/etc/fstab
# 使えるメモリ（MemAvailable）を読む場所
MEMINFO=/proc/meminfo
MIB=$((1024 * 1024))
# swap を止める前に、swap に出ている量に足して残っているべきメモリ
MEMORY_MARGIN=$((256 * MIB))

# SWAPFILE が swap として使われているか。--raw は名前の空白を \x20 にするので、使って
# いるうちに消されたファイル（/swapfile (deleted)）は当たらない
is_active() {
    swapon --show=NAME --noheadings --raw |
        awk -v f="$SWAPFILE" '$1 == f {found = 1} END {exit !found}'
}

# SWAPFILE の swap を止める。swap に出ているものが、余裕を持ってメモリに収まらなければ、
# 止めずに失敗する。収まらないまま止めると、OOM killer がほかのプロセスを止めかねない。
# $1 は、再起動したあとにやり直すこと。誤りの頭には、実行しているスクリプトの名前を付ける
turn_off() {
    local used available
    used=$(swapon --show=NAME,USED --bytes --noheadings --raw |
        awk -v f="$SWAPFILE" '$1 == f {print $2}')
    available=$(($(awk '$1 == "MemAvailable:" {print $2}' "$MEMINFO") * 1024))
    if [ $((used + MEMORY_MARGIN)) -gt "$available" ]; then
        echo "${0##*/}: $SWAPFILE has $((used / MIB)) MiB swapped out, but only" \
            "$((available / MIB)) MiB of memory is available; reboot the instance, then $1" >&2
        exit 1
    fi
    swapoff "$SWAPFILE"
}

# FSTAB にある SWAPFILE の行を $1 の 1 行にする。$1 が空なら消す。ほかの行はそのまま残す。
# 変えたときだけ書き換え、systemd に読み直させる。途中で止まっても、書きかけのファイルを
# /etc に残さない
set_fstab() {
    fstab_tmp=$(mktemp "$FSTAB.XXXXXX")
    trap 'rm -f "$fstab_tmp"' EXIT
    awk -v f="$SWAPFILE" '$1 != f' "$FSTAB" >"$fstab_tmp"
    if [ -n "$1" ]; then
        echo "$1" >>"$fstab_tmp"
    fi
    if ! cmp -s "$fstab_tmp" "$FSTAB"; then
        chmod --reference="$FSTAB" "$fstab_tmp"
        mv "$fstab_tmp" "$FSTAB"
        systemctl daemon-reload
    fi
    rm -f "$fstab_tmp"
    trap - EXIT
}
