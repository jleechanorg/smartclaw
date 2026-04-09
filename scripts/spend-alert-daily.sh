#!/usr/bin/env bash
#
# Multi-source spend alert (launchd): GitHub Actions (billing API), optional Gemini & GCP.
# Canonical copy: https://github.com/jleechanorg/jleechanclaw (use gh CLI; no browser).
#
# Daily thresholds (incremental MTD delta since last successful run):
#   - GitHub Actions:  SPEND_ALERT_GH_DAILY_USD   (default 10)
#   - Gemini:          SPEND_ALERT_GEMINI_DAILY_USD (default 5)
#   - GCP (total):     SPEND_ALERT_GCP_DAILY_USD    (default 5)
#
# Rolling 7-day sums (sum of last up to 7 daily deltas for each source):
#   - GitHub:   SPEND_ALERT_GH_WEEKLY_USD   (default 70 = 7 × 10)
#   - Gemini:   SPEND_ALERT_GEMINI_WEEKLY_USD (default 35 = 7 × 5)
#   - GCP:      SPEND_ALERT_GCP_WEEKLY_USD    (default 35 = 7 × 5)
#
# GitHub: gh api orgs/<GITHUB_ORG>/settings/billing/usage — sums netAmount for
#         current calendar month bucket, product == "actions".
#
# Gemini / GCP: not exposed by a single stable gh command. Provide either:
#   - GEMINI_SPEND_CMD / GCP_SPEND_CMD: shell that prints one MTD USD number on stdout, or
#   - GEMINI_MTD_USD_FILE / GCP_MTD_USD_FILE: JSON {"usd": N} or {"mtd_usd": N}, or a plain number file.
#
# Alerts: Slack (OPENCLAW_SLACK_BOT_TOKEN + SLACK_CHANNEL) when a threshold is exceeded.
#
set -euo pipefail

GITHUB_ORG="${GITHUB_ORG:-jleechanorg}"

SPEND_ALERT_GH_DAILY_USD="${SPEND_ALERT_GH_DAILY_USD:-10}"
SPEND_ALERT_GEMINI_DAILY_USD="${SPEND_ALERT_GEMINI_DAILY_USD:-5}"
SPEND_ALERT_GCP_DAILY_USD="${SPEND_ALERT_GCP_DAILY_USD:-5}"

SPEND_ALERT_GH_WEEKLY_USD="${SPEND_ALERT_GH_WEEKLY_USD:-70}"
SPEND_ALERT_GEMINI_WEEKLY_USD="${SPEND_ALERT_GEMINI_WEEKLY_USD:-35}"
SPEND_ALERT_GCP_WEEKLY_USD="${SPEND_ALERT_GCP_WEEKLY_USD:-35}"

SLACK_CHANNEL="${SLACK_CHANNEL:-C09GRLXF9GR}"

STATE_DIR="${STATE_DIR:-$HOME/.openclaw/state}"
STATE_FILE="${STATE_FILE:-$STATE_DIR/spend-alert-state.json}"

LOG_DIR="${LOG_DIR:-$HOME/.openclaw/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/spend-alert-daily.log}"

mkdir -p "$STATE_DIR" "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $1" | tee -a "$LOG_FILE"
}

log_warn() {
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] WARN: $1" | tee -a "$LOG_FILE"
}

resolve_slack_token() {
    if [[ -n "${OPENCLAW_SLACK_BOT_TOKEN:-}" ]]; then
        printf '%s' "$OPENCLAW_SLACK_BOT_TOKEN"
        return 0
    fi
    if [[ -f "$HOME/.bashrc" ]]; then
        local token
        token=$(
            set +e
            bash -c 'source "$HOME/.bashrc" 2>/dev/null; printf "%s" "${OPENCLAW_SLACK_BOT_TOKEN:-}"'
        )
        if [[ -n "$token" ]]; then
            printf '%s' "$token"
            return 0
        fi
    fi
    return 1
}

send_slack_alert() {
    local message="$1"
    local slack_token
    slack_token=$(resolve_slack_token) || {
        log_warn "Cannot send Slack alert: OPENCLAW_SLACK_BOT_TOKEN not available"
        return 0
    }
    local payload
    payload=$(jq -n --arg channel "$SLACK_CHANNEL" --arg text "$message" \
        '{channel: $channel, text: $text}')
    local response
    response=$(curl -s -X POST "https://slack.com/api/chat.postMessage" \
        -H "Authorization: Bearer $slack_token" \
        -H "Content-Type: application/json" \
        -d "$payload")
    echo "$response" | jq -e '.ok == true' >/dev/null 2>&1 || {
        log_warn "Slack API error: $(echo "$response" | jq -r '.error // "unknown"')"
    }
}

# Sum Actions netAmount for the current UTC month bucket (matches GitHub billing usage API).
github_actions_mtd_usd() {
    local bucket
    bucket=$(date -u '+%Y-%m-01T00:00:00Z')
    gh api "orgs/${GITHUB_ORG}/settings/billing/usage" --paginate 2>/dev/null | \
        jq -s --arg b "$bucket" \
            '[.[].usageItems[]? | select(.date == $b) | select(.product == "actions") | .netAmount] | add // 0'
}

# Returns one line: float or empty if unavailable / invalid.
read_file_mtd_usd() {
    local path="$1"
    [[ -n "$path" && -f "$path" ]] || return 1
    if jq -e . "$path" >/dev/null 2>&1; then
        jq -r '(.usd // .mtd_usd // .gemini_mtd_usd // .gcp_mtd_usd // empty) | select(type == "number")' \
            "$path" 2>/dev/null | head -1
    else
        tr -d '[:space:]' <"$path" | head -c 32
    fi
}

run_mtd_cmd() {
    local cmd="$1"
    [[ -n "$cmd" ]] || return 1
    local out
    out=$(bash -lc "$cmd" 2>/dev/null | head -1 | tr -d '\r\n')
    [[ "$out" =~ ^[0-9]+(\.[0-9]*)?$ ]] && printf '%s' "$out" && return 0
    return 1
}

gemini_mtd_usd() {
    if [[ -n "${GEMINI_SPEND_CMD:-}" ]]; then
        run_mtd_cmd "$GEMINI_SPEND_CMD" && return 0
    fi
    if [[ -n "${GEMINI_MTD_USD_FILE:-}" ]]; then
        read_file_mtd_usd "$GEMINI_MTD_USD_FILE" && return 0
    fi
    printf '0'
}

gcp_mtd_usd() {
    if [[ -n "${GCP_SPEND_CMD:-}" ]]; then
        run_mtd_cmd "$GCP_SPEND_CMD" && return 0
    fi
    if [[ -n "${GCP_MTD_USD_FILE:-}" ]]; then
        read_file_mtd_usd "$GCP_MTD_USD_FILE" && return 0
    fi
    printf '0'
}

current_month_key() {
    date -u '+%Y-%m'
}

# jq: compute next state + alert flags from previous state and current MTD totals.
process_state() {
    local prev_json="$1"
    local month="$2"
    local cgh="$3"
    local cgm="$4"
    local cgp="$5"

    jq -n \
        --argjson prev "$prev_json" \
        --arg month "$month" \
        --argjson cgh "$cgh" \
        --argjson cgm "$cgm" \
        --argjson cgp "$cgp" \
        --argjson gh_d "${SPEND_ALERT_GH_DAILY_USD}" \
        --argjson gm_d "${SPEND_ALERT_GEMINI_DAILY_USD}" \
        --argjson gcp_d "${SPEND_ALERT_GCP_DAILY_USD}" \
        --argjson gh_w "${SPEND_ALERT_GH_WEEKLY_USD}" \
        --argjson gm_w "${SPEND_ALERT_GEMINI_WEEKLY_USD}" \
        --argjson gcp_w "${SPEND_ALERT_GCP_WEEKLY_USD}" \
        '
        def cap(x): if x < 0 then 0 else x end;

        ($prev) as $s |

        if ($s | has("initialized") | not) or ($s.initialized != true) then
          {
            initialized: true,
            month: $month,
            gh_mtd: $cgh, gemini_mtd: $cgm, gcp_mtd: $cgp,
            gh_roll: [], gemini_roll: [], gcp_roll: [],
            alert: { first_init: true }
          }
        elif $s.month != $month then
          {
            initialized: true,
            month: $month,
            gh_mtd: $cgh, gemini_mtd: $cgm, gcp_mtd: $cgp,
            gh_delta: cap($cgh),
            gemini_delta: cap($cgm),
            gcp_delta: cap($cgp),
            gh_roll: ([cap($cgh)] | .[-7:]),
            gemini_roll: ([cap($cgm)] | .[-7:]),
            gcp_roll: ([cap($cgp)] | .[-7:]),
            alert: {
                gh_daily: (cap($cgh) > $gh_d),
                gemini_daily: (cap($cgm) > $gm_d),
                gcp_daily: (cap($cgp) > $gcp_d),
                gh_weekly: (([cap($cgh)] | add) > $gh_w),
                gemini_weekly: (([cap($cgm)] | add) > $gm_w),
                gcp_weekly: (([cap($cgp)] | add) > $gcp_w)
              }
          }
        else
          ($s
               | .gh_roll = (.gh_roll // [])
               | .gemini_roll = (.gemini_roll // [])
               | .gcp_roll = (.gcp_roll // [])
               | .gh_delta = cap($cgh - .gh_mtd)
               | .gemini_delta = cap($cgm - .gemini_mtd)
               | .gcp_delta = cap($cgp - .gcp_mtd)
               | .gh_mtd = $cgh
               | .gemini_mtd = $cgm
               | .gcp_mtd = $cgp
               | .gh_roll = ((.gh_roll + [.gh_delta]) | .[-7:])
               | .gemini_roll = ((.gemini_roll + [.gemini_delta]) | .[-7:])
               | .gcp_roll = ((.gcp_roll + [.gcp_delta]) | .[-7:])
               | .month = $month
               | .initialized = true
               | .alert = {
                   gh_daily: (.gh_delta > $gh_d),
                   gemini_daily: (.gemini_delta > $gm_d),
                   gcp_daily: (.gcp_delta > $gcp_d),
                   gh_weekly: ((.gh_roll | add // 0) > $gh_w),
                   gemini_weekly: ((.gemini_roll | add // 0) > $gm_w),
                   gcp_weekly: ((.gcp_roll | add // 0) > $gcp_w)
                 }
          )
        end
        '
}

build_slack_message() {
    local result_json="$1"
    echo "$result_json" | jq -r \
        --arg gh_d "$SPEND_ALERT_GH_DAILY_USD" \
        --arg gm_d "$SPEND_ALERT_GEMINI_DAILY_USD" \
        --arg gcp_d "$SPEND_ALERT_GCP_DAILY_USD" \
        --arg gh_w "$SPEND_ALERT_GH_WEEKLY_USD" \
        --arg gm_w "$SPEND_ALERT_GEMINI_WEEKLY_USD" \
        --arg gcp_w "$SPEND_ALERT_GCP_WEEKLY_USD" \
        '
      . as $r |
      if ($r.alert.first_init == true) then
        empty
      else
        [
          "[Spend Alert] Thresholds (daily or 7-day rolling):",
          (if ($r.alert.gh_daily // false) then
            "- GitHub Actions daily Δ $" + ($r.gh_delta | tostring) + " > $" + $gh_d + " (MTD $" + ($r.gh_mtd | tostring) + ")"
           else empty end),
          (if ($r.alert.gemini_daily // false) then
            "- Gemini daily Δ $" + ($r.gemini_delta | tostring) + " > $" + $gm_d + " (MTD $" + ($r.gemini_mtd | tostring) + ")"
           else empty end),
          (if ($r.alert.gcp_daily // false) then
            "- GCP daily Δ $" + ($r.gcp_delta | tostring) + " > $" + $gcp_d + " (MTD $" + ($r.gcp_mtd | tostring) + ")"
           else empty end),
          (if ($r.alert.gh_weekly // false) then
            "- GitHub Actions 7d sum $" + (($r.gh_roll | add // 0) | tostring) + " > $" + $gh_w
           else empty end),
          (if ($r.alert.gemini_weekly // false) then
            "- Gemini 7d sum $" + (($r.gemini_roll | add // 0) | tostring) + " > $" + $gm_w
           else empty end),
          (if ($r.alert.gcp_weekly // false) then
            "- GCP 7d sum $" + (($r.gcp_roll | add // 0) | tostring) + " > $" + $gcp_w
           else empty end)
        ] | map(select(. != null and . != "")) |
          if map(select(test("^-"))) | length == 0 then empty else join("\n") end
      end
    '
}

main() {
    log "=== spend-alert-daily started ==="

    if ! command -v gh >/dev/null 2>&1; then
        log_warn "gh not in PATH"
        exit 1
    fi
    if ! command -v jq >/dev/null 2>&1; then
        log_warn "jq not in PATH"
        exit 1
    fi

    export SPEND_ALERT_GH_DAILY_USD SPEND_ALERT_GEMINI_DAILY_USD SPEND_ALERT_GCP_DAILY_USD
    export SPEND_ALERT_GH_WEEKLY_USD SPEND_ALERT_GEMINI_WEEKLY_USD SPEND_ALERT_GCP_WEEKLY_USD

    local cgh cgm cgp
    cgh=$(github_actions_mtd_usd) || {
        log_warn "Failed to fetch GitHub billing usage (check gh auth)"
        exit 1
    }
    cgm=$(gemini_mtd_usd) || cgm="0"
    cgp=$(gcp_mtd_usd) || cgp="0"

    log "MTD snapshot: gh_actions=$cgh gemini=$cgm gcp=$cgp (org=$GITHUB_ORG)"

    local month
    month=$(current_month_key)

    local prev
    prev=$( (cat "$STATE_FILE" 2>/dev/null || echo '{"initialized":false}') | jq -c . )

    local result
    result=$(process_state "$prev" "$month" "$cgh" "$cgm" "$cgp")

    echo "$result" | jq . >"$STATE_FILE"
    log "State updated at $STATE_FILE"

    if echo "$result" | jq -e '.alert.first_init == true' >/dev/null 2>&1; then
        log "First run: initialized; no alerts until next execution."
        log "=== spend-alert-daily finished ==="
        exit 0
    fi

    local msg
    msg=$(build_slack_message "$result")

    if [[ -n "${msg:-}" ]]; then
        log "ALERT: sending Slack notification"
        send_slack_alert "$msg"
    else
        log "No thresholds exceeded."
    fi

    log "=== spend-alert-daily finished ==="
}

main "$@"
