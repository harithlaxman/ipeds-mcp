#!/usr/bin/env bash
#
# Download IPEDS Access DB zips and extract the .accdb file from each.
#
# IPEDS Access DBs are published per academic year (2004-05 .. 2023-24) at:
#   https://nces.ed.gov/ipeds/tablefiles/zipfiles/IPEDS_<YYYY>-<YY+1>_Final.zip
# Each zip holds one .accdb (e.g. IPEDS200405.accdb) plus other files we don't
# need. We keep only the .accdb and drop everything else.
#
# Usage:
#   ./download.sh <year>            # one year, e.g. 2011
#   ./download.sh -all              # all years 2004..2023
#   ./download.sh <year|-all> -o <dir>   # output dir (default: ./data)

set -euo pipefail

FIRST_YEAR=2004
LAST_YEAR=2023
BASE_URL="https://nces.ed.gov/ipeds/tablefiles/zipfiles"

usage() {
    cat >&2 <<EOF
Usage: $(basename "$0") (<year> | -all) [-o <out_dir>]

  <year>          a single year to download, ${FIRST_YEAR}..${LAST_YEAR}
  -all, --all     download every year from ${FIRST_YEAR} to ${LAST_YEAR}
  -o, --out <dir> output directory for .accdb files (default: ./data)
EOF
    exit 2
}

# --- parse args ------------------------------------------------------------
YEAR=""
ALL=false
OUT="data/accdb"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -all|--all)
            ALL=true
            shift
            ;;
        -o|--out)
            [[ $# -ge 2 ]] || { echo "error: $1 needs a directory argument" >&2; usage; }
            OUT="$2"
            shift 2
            ;;
        [0-9][0-9][0-9][0-9])
            [[ -z "$YEAR" ]] || { echo "error: multiple years given" >&2; usage; }
            YEAR="$1"
            shift
            ;;
        *)
            echo "error: unrecognized argument: $1" >&2
            usage
            ;;
    esac
done

# Require exactly one of <year> / -all.
if $ALL && [[ -n "$YEAR" ]]; then
    echo "error: give either a year or -all, not both" >&2
    usage
fi
if ! $ALL && [[ -z "$YEAR" ]]; then
    echo "error: provide a year or -all" >&2
    usage
fi
if [[ -n "$YEAR" ]] && { (( YEAR < FIRST_YEAR )) || (( YEAR > LAST_YEAR )); }; then
    echo "error: year must be between ${FIRST_YEAR} and ${LAST_YEAR}" >&2
    usage
fi

# --- download one year -----------------------------------------------------
download_year() {
    local year="$1"
    local y1 url tmp
    y1=$(printf '%02d' $(( (year + 1) % 100 )))
    url="${BASE_URL}/IPEDS_${year}-${y1}_Final.zip"

    tmp=$(mktemp -d)
    # Clean the scratch dir even if curl/unzip fails partway through.
    trap 'rm -rf "$tmp"' RETURN

    echo "==> ${year}-${y1}: downloading"
    if ! curl -fSL --retry 3 -o "$tmp/ipeds.zip" "$url"; then
        echo "warning: failed to download ${url}" >&2
        return 1
    fi

    # -j junk paths (flatten), -o overwrite; the *.accdb filter keeps only the
    # Access DB and leaves everything else in the zip behind.
    unzip -o -j "$tmp/ipeds.zip" '*.accdb' -d "$OUT"
    echo "==> ${year}-${y1}: done"
}

# --- main ------------------------------------------------------------------
mkdir -p "$OUT"

if $ALL; then
    failures=0
    for year in $(seq "$FIRST_YEAR" "$LAST_YEAR"); do
        download_year "$year" || failures=$((failures + 1))
    done
    if (( failures > 0 )); then
        echo "finished with ${failures} failed year(s)" >&2
        exit 1
    fi
    echo "All years downloaded into ${OUT}/"
else
    download_year "$YEAR"
    echo "Downloaded into ${OUT}/"
fi
