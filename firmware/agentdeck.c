/* SPDX-License-Identifier: GPL-3.0-only */
/* Agent Deck: the FM-1 as a control surface for CLI coding agents.
 *
 * A Dinghy app (platform.c below is Felucca/SLOOP's, via Dinghy — see
 * LICENSING.md in the Dinghy tree). The synth engine is gone: app_audio is
 * silent. The panel talks to a host daemon (host/agentdeck.py) over USB-MIDI
 * channel 16; the wire format is PROTOCOL.md in this project. In short:
 *
 *   out: keys = Note On/Off 48..74 (a press also selects the slot),
 *        buttons = CC 102..115, encoders = CC 85..91 (relative),
 *        MASTER = CC 7, CC 116 = 1 s heartbeat, CC 117 = proto version,
 *        CC 120 = the selected slot, when it changes here.
 *   in:  a framed byte stream, CC 118 = frame control (0x10|type start,
 *        0x20 end, 0x7F reset), CC 119 = one payload byte. SysEx never
 *        reaches an app in this platform (the updater owns it), hence CCs.
 *
 * Pages (ALGORITHM turns, GLO/HOME/EDIT jump): GRID (the slot list),
 * DETAIL (the selected agent's last lines; K1 scrolls), APPROVALS
 * (the BLOCKED slots). OCT-/OCT+ page the 54 slots, 27 keys at a time.
 * Settings kept in flash: the selected slot and the page.
 *
 * Rules of the road (the chip has no FPU and no libgcc): integers only,
 * no float, no 64-bit division. tests/host.c runs this file on a computer.
 */
#define APP_NAME "AGTDECK"
#define CV_MAX (240u * 192u)                    /* one big canvas: the list area */
static void app_idle(void);
#define LCD_IDLE app_idle                       /* while the screen's DMA runs: MIDI in (platform.c) */
#include "platform.c"

/* ---- the sound: none. Agent Deck is a controller, not a synth. */
static void app_audio(int32_t *out, uint32_t n)
{
    (void)out;
    (void)n;
}

/* ---- protocol (PROTOCOL.md) */
#define PROTO_VER 1u
#define CH16 0x0Fu                              /* channel 16, as a status low nibble */
#define ST_NOTEON 0x9Fu
#define ST_NOTEOFF 0x8Fu
#define ST_CC 0xBFu
#define CC_FRAME_CTRL 118u
#define CC_FRAME_DATA 119u
#define CC_HEARTBEAT 116u
#define CC_HELLO 117u
#define CC_SELECTION 120u
#define FRAME_END 0x20u
#define FRAME_RESET 0x7Fu
#define FT_SLOT 0x01u
#define FT_DETAIL 0x02u
#define FT_META 0x03u
#define FT_HELLO_ACK 0x0Fu

#define MAXSLOTS 54u                            /* two key-pages of 27 */
#define NAME_CHARS 12u
#define DETAIL_LINES 8u
#define DETAIL_CHARS 29u
#define PAGE_GRID 0u
#define PAGE_DETAIL 1u
#define PAGE_APPROVALS 2u

enum { S_EMPTY, S_IDLE, S_WORKING, S_BLOCKED, S_DONE, S_ERROR, S_UNKNOWN };

static uint8_t slot_state[MAXSLOTS];
static uint8_t slot_kind[MAXSLOTS];
static char slot_name[MAXSLOTS][NAME_CHARS + 1u];
static uint32_t slot_done_t[MAXSLOTS];          /* fm1_ms when DONE arrived */
static char detail[DETAIL_LINES][DETAIL_CHARS + 1u];
static uint8_t meta_agents, meta_blocked, host_seen;
static uint8_t sel_slot, page, slot_page;
static uint8_t detail_scroll;
static uint32_t ui_dirty = 1, drawn_phase = ~0u, drawn_header = ~0u;

/* ---- the frame receiver (host -> device) */
static uint8_t rx_type, rx_len, rx_active;
static uint8_t rx_buf[64];

static void slots_clear(void)
{
    uint32_t i, j;
    for (i = 0; i < MAXSLOTS; i++) {
        slot_state[i] = S_EMPTY;
        slot_kind[i] = 0;
        for (j = 0; j <= NAME_CHARS; j++) slot_name[i][j] = 0;
    }
    for (i = 0; i < DETAIL_LINES; i++) detail[i][0] = 0;
    meta_agents = 0;
    meta_blocked = 0;
    ui_dirty = 1;
}

static void rx_commit(void)
{
    uint32_t i;
    host_seen = 1;
    if (rx_type == FT_SLOT && rx_len >= 3u) {
        uint32_t s = rx_buf[0];
        if (s < MAXSLOTS) {
            uint8_t st = rx_buf[1] <= S_UNKNOWN ? rx_buf[1] : S_UNKNOWN;
            if (st == S_DONE && slot_state[s] != S_DONE) slot_done_t[s] = fm1_ms;
            slot_state[s] = st;
            slot_kind[s] = rx_buf[2] <= 6u ? rx_buf[2] : 0u;
            for (i = 0; i < NAME_CHARS; i++) {
                uint32_t c = 3u + i < rx_len ? rx_buf[3u + i] : 0u;
                slot_name[s][i] = c >= 32u && c <= 126u ? (char)c : 0;
                if (!c) break;
            }
            slot_name[s][NAME_CHARS] = 0;
        }
    } else if (rx_type == FT_DETAIL && rx_len >= 1u) {
        uint32_t ln = rx_buf[0];
        if (ln < DETAIL_LINES) {
            for (i = 0; i < DETAIL_CHARS; i++) {
                uint32_t c = 1u + i < rx_len ? rx_buf[1u + i] : 0u;
                detail[ln][i] = c >= 32u && c <= 126u ? (char)c : 0;
                if (!c) break;
            }
            detail[ln][DETAIL_CHARS] = 0;
        }
    } else if (rx_type == FT_META && rx_len >= 2u) {
        meta_agents = rx_buf[0];
        meta_blocked = rx_buf[1];
    }
    ui_dirty = 1;
}

static void midi_in(void)
{
    uint8_t st, d1, d2;
    int src;
    while ((src = midi_get(&st, &d1, &d2)) != 0) {
        (void)src;
        if ((st & 0xF0u) != 0xB0u || (st & 0x0Fu) != CH16) continue;   /* only our CCs */
        if (d1 == CC_FRAME_CTRL) {
            if (d2 == FRAME_RESET) { slots_clear(); rx_active = 0; }
            else if (d2 == FRAME_END) { if (rx_active) rx_commit(); rx_active = 0; }
            else if ((d2 & 0xF0u) == 0x10u) { rx_type = d2 & 0x0Fu; rx_len = 0; rx_active = 1; }
        } else if (d1 == CC_FRAME_DATA && rx_active) {
            if (rx_len < sizeof rx_buf) rx_buf[rx_len++] = d2;
        }
    }
}
static uint32_t idle_ok;
static void app_idle(void)
{
    if (idle_ok) midi_in();
}

/* ---- small text helpers (no printf on this chip) */
static const char *const STATE_NAME[7] = {"EMPTY", "IDLE", "WORKING", "BLOCKED", "DONE", "ERROR", "UNKNOWN"};
static const char *const KIND_NAME[7] = {"UNKNOWN", "CLAUDE", "CODEX", "OPENCODE", "GROK", "COPILOT", "SHELL"};
static char state_glyph(uint8_t s)
{
    return s == S_IDLE ? '.' : s == S_WORKING ? '>' : s == S_BLOCKED ? '!' :
           s == S_DONE ? '*' : s == S_ERROR ? 'x' : s == S_UNKNOWN ? '?' : '-';
}
static void put2(char *p, uint32_t v)           /* two decimal digits */
{
    p[0] = (char)('0' + (v / 10u) % 10u);
    p[1] = (char)('0' + v % 10u);
}
static char *put_str_r(char *dst, const char *src)   /* copy, return the end */
{
    while (*src) *dst++ = *src++;
    *dst = 0;
    return dst;
}

/* ---- the screen */
#define C_ACC RGB(120, 200, 255)
#define C_TXT RGB(190, 190, 190)
#define C_GREY RGB(100, 100, 100)
#define C_WARN RGB(255, 180, 60)
#define C_BAD RGB(255, 90, 70)
#define C_GOOD RGB(120, 255, 146)

static uint16_t state_colour(uint8_t s)
{
    return s == S_BLOCKED ? C_WARN : s == S_ERROR ? C_BAD :
           s == S_DONE ? C_GOOD : s == S_WORKING ? C_TXT : C_GREY;
}

static void draw_header(void)
{
    char s[40], *p = s;
    draw_text_box(0, 2, 240, &FONT_L, APP_NAME, C_ACC, 1);
    p = put_str_r(p, "AGENTS ");
    *p++ = (char)('0' + meta_agents / 10u);
    *p++ = (char)('0' + meta_agents % 10u);
    p = put_str_r(p, "  BLOCKED ");
    *p++ = (char)('0' + meta_blocked % 10u);
    p = put_str_r(p, host_seen ? "  HOST OK" : "  NO HOST");
    *p = 0;
    draw_text_box(0, 38, 240, &FONT_S, s, meta_blocked ? C_WARN : C_GREY, 1);
}

static void draw_row(int32_t y, uint32_t slot, int32_t selected)
{
    char s[32], *p = s;
    *p++ = selected ? '>' : ' ';
    *p++ = ' ';
    put2(p, slot); p += 2;
    *p++ = ' ';
    *p++ = state_glyph(slot_state[slot]);
    *p++ = ' ';
    p = put_str_r(p, slot_name[slot][0] ? slot_name[slot] : "(empty)");
    *p = 0;
    cv_text(4, y, &FONT_S, s, selected ? C_WHITE : state_colour(slot_state[slot]));
}

static void draw_content(void)
{
    uint32_t i, n = 0;
    cv_begin(240, 184, C_BLACK);
    if (page == PAGE_DETAIL) {
        char s[40], *p = s;
        cv_text(4, 0, &FONT_S, slot_name[sel_slot][0] ? slot_name[sel_slot] : "(empty)", C_WHITE);
        p = put_str_r(p, STATE_NAME[slot_state[sel_slot]]);
        p = put_str_r(p, "  ");
        p = put_str_r(p, KIND_NAME[slot_kind[sel_slot] <= 6u ? slot_kind[sel_slot] : 0u]);
        *p = 0;
        cv_text(4, 20, &FONT_S, s, state_colour(slot_state[sel_slot]));
        for (i = 0; i < 5u && detail_scroll + i < DETAIL_LINES; i++)
            cv_text(4, 48 + (int32_t)i * 20, &FONT_S, detail[detail_scroll + i], C_TXT);
        cv_text(4, 160, &FONT_S, "K1 SCROLLS - REC APPROVES", C_GREY);
    } else if (page == PAGE_APPROVALS) {
        cv_text(4, 0, &FONT_S, "BLOCKED - WAITING ON YOU", C_WARN);
        for (i = 0; i < MAXSLOTS && n < 8u; i++)
            if (slot_state[i] == S_BLOCKED) { draw_row(24 + (int32_t)n * 20, i, i == sel_slot); n++; }
        if (!n) cv_text(4, 24, &FONT_S, "(none)", C_GREY);
    } else {                                    /* GRID: this key-page's slots */
        uint32_t base = slot_page * 27u, idx = sel_slot >= base ? sel_slot - base : 0u;
        uint32_t start = idx > 8u ? idx - 8u : 0u;
        for (i = 0; i < 9u && start + i < 27u; i++)
            draw_row((int32_t)i * 20, base + start + i, base + start + i == sel_slot);
    }
    cv_blit(0, 56);
}

/* ---- LEDs */
static uint8_t led_mode(uint32_t slot, uint32_t now)
{
    uint8_t st = slot_state[slot];
    uint32_t phase500 = (now / 500u) & 1u;
    if (st == S_EMPTY) return slot == sel_slot ? (phase500 ? 1u : 0u) : 0u;
    if (st == S_IDLE) return slot == sel_slot && phase500 ? 2u : 1u;
    if (st == S_WORKING) return 2u;
    if (st == S_BLOCKED) return ((now / 250u) & 1u) ? 2u : 0u;
    if (st == S_DONE) return now - slot_done_t[slot] < 2000u ? 2u : 1u;
    if (st == S_ERROR) return ((now / 167u) & 1u) ? 2u : 0u;
    return phase500 ? 1u : 0u;                  /* UNKNOWN: glow pulsing at 1 Hz */
}

static void leds(void)
{
    uint32_t k, now = fm1_ms;
    for (k = 0; k < 27u; k++) {
        uint32_t slot = slot_page * 27u + k;
        uint8_t m = slot < MAXSLOTS ? led_mode(slot, now) : 0u;
        fm1_led_key(KEY_LED(k), m == 2u);
        fm1_led_key_glow(KEY_LED(k), m == 1u);
    }
    fm1_led_key(BTN_PLAY, slot_state[sel_slot] == S_WORKING);
    fm1_led_key(BTN_REC, slot_state[sel_slot] == S_BLOCKED && ((now / 250u) & 1u));
    fm1_led_key(BTN_HOME, page == PAGE_GRID);
    fm1_led_key(BTN_GLO, page == PAGE_APPROVALS);
}

/* ---- selection */
static struct { uint8_t sel, page, slotpage; } set = {0, 0, 0};
static uint32_t set_dirty, set_t;
static void sel_set(uint32_t s)
{
    if (s >= MAXSLOTS || s == sel_slot) return;
    sel_slot = (uint8_t)s;
    midi_send(ST_CC, CC_SELECTION, sel_slot);
    ui_dirty = 1;
    set.sel = sel_slot;
    set_dirty = 1;
    set_t = fm1_ms;
}
static void sel_move(int32_t d)
{
    int32_t s = (int32_t)sel_slot, tries;
    for (tries = 0; tries < (int32_t)MAXSLOTS; tries++) {
        s += d;
        if (s < 0) s += (int32_t)MAXSLOTS;
        if (s >= (int32_t)MAXSLOTS) s -= (int32_t)MAXSLOTS;
        if (slot_state[s] != S_EMPTY) { sel_set((uint32_t)s); return; }
    }
}

static void app_main(void)
{
    uint32_t prev_notes = 0, t_hb = 0, hello_sent = 0;
    int32_t last_master = -2;
    if (settings_load(&set, sizeof set) == 0 && set.sel < MAXSLOTS &&
        set.page <= PAGE_APPROVALS && set.slotpage <= 1u) {
        sel_slot = set.sel;
        page = set.page;
        slot_page = set.slotpage;
    }
    lcd_fill(0, 0, 240, 240, C_BLACK);

    for (;;) {
        uint32_t keys, rel, b, k;
        int32_t e;
        platform_poll();

        if (!hello_sent) {                      /* announce ourselves once the USB stack is up */
            midi_send(ST_CC, CC_HELLO, PROTO_VER);
            hello_sent = 1;
        }
        if (fm1_ms - t_hb >= 1000u) {
            t_hb = fm1_ms;
            midi_send(ST_CC, CC_HEARTBEAT, 1u);
        }

        /* keys: a press selects the slot and goes out as a note; releases as
         * note offs. Level-diff like dinghy.c (the host harness stubs
         * fm1_input_note_edges() to 0, and the panel is debounced anyway). */
        keys = fm1_in.notes;
        for (k = 0; k < 27u; k++) {
            if ((keys >> k & 1u) != 0u && (prev_notes >> k & 1u) == 0u) {
                sel_set(slot_page * 27u + k);
                midi_send(ST_NOTEON, 48u + k, 127u);
            }
            if ((keys >> k & 1u) == 0u && (prev_notes >> k & 1u) != 0u)
                midi_send(ST_NOTEOFF, 48u + k, 0u);
        }
        prev_notes = keys;

        /* buttons: out as CC 102+id; a few also act locally (page jumps) */
        b = fm1_input_edges(&rel);
        for (k = 0; k < 14u; k++) {
            if (b >> k & 1u) midi_send(ST_CC, 102u + k, 127u);
            if (rel >> k & 1u) midi_send(ST_CC, 102u + k, 0u);
        }
        if (b & (1u << BTN_HOME)) { page = PAGE_GRID; ui_dirty = 1; }
        if (b & (1u << BTN_GLO)) { page = PAGE_APPROVALS; ui_dirty = 1; }
        if (b & (1u << BTN_EDIT)) { page = PAGE_DETAIL; ui_dirty = 1; }
        if (b & (1u << BTN_SAVE)) { page = PAGE_APPROVALS; ui_dirty = 1; }
        if (b & (1u << BTN_OCT_DOWN) && slot_page > 0u) slot_page--;
        if (b & (1u << BTN_OCT_UP) && slot_page < 1u) slot_page++;
        if (b & ((1u << BTN_OCT_DOWN) | (1u << BTN_OCT_UP))) {
            if (sel_slot / 27u != slot_page) sel_set(slot_page * 27u);
            set.page = page; set.slotpage = slot_page; set_dirty = 1; set_t = fm1_ms;
            ui_dirty = 1;
        }
        if (b & ((1u << BTN_HOME) | (1u << BTN_GLO) | (1u << BTN_EDIT) | (1u << BTN_SAVE))) {
            set.page = page; set_dirty = 1; set_t = fm1_ms;
        }

        /* encoders: out as relative CCs; SELECT / ALGORITHM / K1 also act locally */
        {
            static const uint8_t ENC_ID[7] = {ENC_SELECT, ENC_ALGORITHM, ENC_KNOB1,
                                              ENC_KNOB2, ENC_KNOB3, ENC_KNOB4, ENC_PRESETS};
            uint32_t i;
            for (i = 0; i < 7u; i++) {
                e = fm1_enc_take(ENC_ID[i]);
                if (!e) continue;
                if (e > 63) e = 63;
                if (e < -63) e = -63;
                midi_send(ST_CC, 85u + i, (uint32_t)(64 + e));
                if (ENC_ID[i] == ENC_SELECT) sel_move(e > 0 ? 1 : -1);
                else if (ENC_ID[i] == ENC_ALGORITHM) {
                    page = (uint8_t)((page + (e > 0 ? 1u : 2u)) % 3u);
                    set.page = page; set_dirty = 1; set_t = fm1_ms; ui_dirty = 1;
                } else if (ENC_ID[i] == ENC_KNOB1) {
                    int32_t sc = (int32_t)detail_scroll + e;
                    detail_scroll = (uint8_t)(sc < 0 ? 0 : sc > 3 ? 3 : sc);
                    ui_dirty = 1;
                }
            }
        }

        /* MASTER pot: the platform owns the volume; we just report it.
         * (The host test harness has no ADC, so this is device-only.) */
#ifndef DINGHY_HOST
        {
            int32_t v = fm1_adc_read(FM1_ADC_MASTER);
            if (v >= 0) {
                int32_t cc = v >> 3;
                if (cc - last_master >= 2 || last_master - cc >= 2) {
                    last_master = cc;
                    midi_send(ST_CC, 7u, (uint32_t)cc);
                }
            }
        }
#endif

        midi_in();
        leds();

        idle_ok = 1;
        {
            uint32_t header_sig = (uint32_t)meta_agents << 8 | meta_blocked |
                                  (host_seen ? (1u << 16) : 0u) | (uint32_t)page << 20;
            uint32_t phase = (fm1_ms / 250u) & 1u;
            int32_t blink_active = slot_state[sel_slot] == S_BLOCKED || meta_blocked != 0u;
            if (header_sig != drawn_header) { drawn_header = header_sig; draw_header(); }
            if (ui_dirty || (blink_active && phase != drawn_phase)) {
                ui_dirty = 0;
                drawn_phase = phase;
                draw_content();
            }
        }
        idle_ok = 0;

        if (set_dirty && fm1_ms - set_t > 2000u) {   /* flash wears: save late, once */
            set.sel = sel_slot;
            settings_save(&set, sizeof set);
            set_dirty = 0;
        }
    }
}
