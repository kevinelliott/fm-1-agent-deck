/* SPDX-License-Identifier: GPL-3.0-only */
/* Agent Deck: the FM-1 as a control surface for CLI coding agents.
 *
 * A Dinghy app (platform.c below is Felucca/SLOOP's, via Dinghy — see
 * LICENSING.md in the Dinghy tree). The synth engine is gone; the only
 * sound left is a handful of alert chimes the host asks for. The panel
 * talks to a host daemon (host/agentdeck.py) over USB-MIDI channel 16; the
 * wire format is PROTOCOL.md in this project (this is protocol v4). In short:
 *
 *   out: keys = Note On/Off 48..74 (a press also selects the slot),
 *        buttons = CC 102..115, encoders = CC 85..91 (relative),
 *        MASTER = CC 7, CC 116 = 1 s heartbeat (value = protocol version),
 *        CC 117 = hello (protocol version), CC 120 = the selected slot
 *        and CC 121 = the page, when they change here or when the host
 *        says hello.
 *   in:  a framed byte stream, CC 118 = frame control (0x10|type start,
 *        0x20 end, 0x7F reset), CC 119 = one payload byte. SysEx never
 *        reaches an app in this platform (the updater owns it), hence CCs.
 *        Frames: SLOT, DETAIL, META, FOOTER, BEEP, SELECT, TEXT, PAGE,
 *        KEYLEDS, BTNLEDS, HELLO_ACK.
 *
 * The device is deliberately dumb: the host owns everything that needs
 * judgement (what a knob does to an agent, which prompt is armed, when to
 * chime, how far the detail text is scrolled) and the device renders it.
 * What stays local is what must feel instant: the selection cursor, the
 * page (ALGORITHM turns, GLO/HOME/EDIT jump), the LEDs, the clocks.
 *
 * Pages: GRID (the slot list, with how long each agent has been in its
 * state), DETAIL (the selected agent's output, as the host windows it),
 * APPROVALS (the BLOCKED slots), MENU (eight lines of text the host
 * writes: its menus; SELECT is the host's there). OCT-/OCT+ page the 54
 * slots, 27 keys at a time. A key pressed while a button is held is a
 * chord: it goes to the host without moving the cursor, and while the
 * button is held the host may light the keys its own way (KEYLEDS). The bottom line of every page is the host's footer (the armed
 * prompt, the result of the last action). Settings kept in flash: the
 * selected slot and the page.
 *
 * Rules of the road (the chip has no FPU and no libgcc): integers only,
 * no float, no 64-bit division. tests/host.c runs this file on a computer.
 */
#define APP_NAME "AGTDECK"
#define CV_MAX (240u * 192u)                    /* one big canvas: the list area */
static void app_idle(void);
#define LCD_IDLE app_idle                       /* while the screen's DMA runs: MIDI in (platform.c) */
#include "platform.c"

/* ---- the sound: alert chimes only. The host asks for one with a BEEP
 * frame; the main loop hands it to the audio interrupt through beep_seq /
 * beep_id (one writer: the main loop). MASTER is the volume (platform.c).
 * A tone is a triangle with a linear decay; {phase step, frames, decay}:
 * step = f * 2^32 / 44100, decay = (BEEP_AMP << 8) / frames, both worked
 * out here so the interrupt never divides. step 0 is a rest. */
#define BEEP_AMP 12000
typedef struct { uint32_t inc; uint16_t n; uint16_t dec; } tone_t;
static const tone_t PAT_ATTENTION[] = {         /* an agent needs you: two rising calls */
    {85704563u, 3969, 773}, {114435070u, 3969, 773}, {0, 2646, 0},
    {85704563u, 3969, 773}, {114435070u, 6174, 497}, {0, 0, 0}};
static const tone_t PAT_DONE[] = {              /* an agent finished: C E G */
    {50960128u, 3528, 870}, {64205378u, 3528, 870}, {76354000u, 7056, 435}, {0, 0, 0}};
static const tone_t PAT_ERROR[] = {             /* something broke: two low notes */
    {19088744u, 7056, 435}, {0, 2646, 0}, {19088744u, 11466, 267}, {0, 0, 0}};
static const tone_t PAT_ACK[] = {{152709948u, 1544, 1989}, {0, 0, 0}};   /* a click: done as told */
static const tone_t *const PATTERNS[4] = {PAT_ATTENTION, PAT_DONE, PAT_ERROR, PAT_ACK};
static volatile uint32_t beep_seq;
static volatile uint8_t beep_id;

static void app_audio(int32_t *out, uint32_t n)
{
    static const tone_t *tn;
    static uint32_t seen, ph, inc, env, dec, left;
    uint32_t i;
    if (beep_seq != seen) {
        uint32_t id = beep_id;
        seen = beep_seq;
        tn = id >= 1u && id <= 4u ? PATTERNS[id - 1u] : 0;
        left = 0;
    }
    if (!tn && !left) return;
    for (i = 0; i < n; i++) {
        if (!left) {
            if (!tn || !tn->n) { tn = 0; return; }
            inc = tn->inc; dec = tn->dec; left = tn->n;
            env = (uint32_t)BEEP_AMP << 8;
            ph = 0;
            tn++;
        }
        left--;
        if (inc) {
            int32_t t, v;
            ph += inc;
            t = (int32_t)(ph >> 17) - 16384;    /* a triangle, -16384..16384 */
            if (t < 0) t = -t;
            t = 2 * t - 16384;
            v = t * (int32_t)(env >> 8) / 16384;
            out[2u * i] += v;
            out[2u * i + 1u] += v;
            env = env > dec ? env - dec : 0u;
        }
    }
}

/* ---- protocol (PROTOCOL.md) */
#define PROTO_VER 4u
#define CH16 0x0Fu                              /* channel 16, as a status low nibble */
#define ST_NOTEON 0x9Fu
#define ST_NOTEOFF 0x8Fu
#define ST_CC 0xBFu
#define CC_FRAME_CTRL 118u
#define CC_FRAME_DATA 119u
#define CC_HEARTBEAT 116u
#define CC_HELLO 117u
#define CC_SELECTION 120u
#define CC_PAGE 121u
#define FRAME_END 0x20u
#define FRAME_RESET 0x7Fu
#define FT_SLOT 0x01u
#define FT_DETAIL 0x02u
#define FT_META 0x03u
#define FT_FOOTER 0x04u
#define FT_BEEP 0x05u
#define FT_SELECT 0x06u
#define FT_TEXT 0x07u
#define FT_PAGE 0x08u
#define FT_KEYLEDS 0x09u
#define FT_BTNLEDS 0x0Au
#define FT_HELLO_ACK 0x0Fu
#define KIND_AUTO 0x40u                         /* SLOT's kind byte: the host auto-approves this agent */
#define KIND_LOOP 0x20u                         /* ... and: the host repeats a prompt at it */

#define MAXSLOTS 54u                            /* two key-pages of 27 */
#define NAME_CHARS 12u
#define DETAIL_LINES 8u                         /* as many as the host may send */
#define DETAIL_SHOWN 6u                         /* as many as fit above the footer */
#define DETAIL_CHARS 29u
#define FOOTER_CHARS 29u
#define GRID_ROWS 8u
#define HOST_TIMEOUT_MS 25000u                  /* no frame for this long: NO HOST (the host resyncs every 10 s) */
#define PAGE_GRID 0u
#define PAGE_DETAIL 1u
#define PAGE_APPROVALS 2u
#define PAGE_MENU 3u
#define NPAGES 4u
#define MENU_LINES 8u                           /* a title and seven rows */

enum { S_EMPTY, S_IDLE, S_WORKING, S_BLOCKED, S_DONE, S_ERROR, S_UNKNOWN };

static uint8_t slot_state[MAXSLOTS];
static uint8_t slot_kind[MAXSLOTS];
static uint8_t slot_auto[MAXSLOTS];
static uint8_t slot_loop[MAXSLOTS];
static char slot_name[MAXSLOTS][NAME_CHARS + 1u];
static uint32_t slot_since[MAXSLOTS];           /* fm1_ms when the state last changed */
static char detail[DETAIL_LINES][DETAIL_CHARS + 1u];
static char footer[FOOTER_CHARS + 1u];
static uint8_t footer_style;
static char menu_text[MENU_LINES][DETAIL_CHARS + 1u];
static uint8_t menu_style[MENU_LINES];
static uint8_t kled[27], kled_on;                /* the host's key LEDs, shown while a button is held */
static uint8_t bled[14];                         /* the host's button LEDs: 0 ours, 1 off, 2 on, 3 blink, 4 glow */
static uint8_t announce_page;
static uint8_t meta_agents, meta_blocked, host_seen, announce_sel;
static uint32_t host_t;                         /* fm1_ms of the host's last frame */
static uint8_t sel_slot, page, slot_page;
static uint32_t ui_dirty = 1, drawn_phase = ~0u, drawn_header = ~0u, drawn_sec = ~0u;

static struct { uint8_t sel, page, slotpage; } set = {0, 0, 0};
static uint32_t set_dirty, set_t;

/* ---- the frame receiver (host -> device) */
static uint8_t rx_type, rx_len, rx_active;
static uint8_t rx_buf[64];

static void slots_clear(void)
{
    uint32_t i, j;
    for (i = 0; i < MAXSLOTS; i++) {
        slot_state[i] = S_EMPTY;
        slot_kind[i] = 0;
        slot_auto[i] = 0;
        slot_loop[i] = 0;
        for (j = 0; j <= NAME_CHARS; j++) slot_name[i][j] = 0;
    }
    for (i = 0; i < DETAIL_LINES; i++) detail[i][0] = 0;
    for (i = 0; i < MENU_LINES; i++) menu_text[i][0] = 0;
    for (i = 0; i < 14u; i++) bled[i] = 0;
    kled_on = 0;
    footer[0] = 0;
    meta_agents = 0;
    meta_blocked = 0;
    ui_dirty = 1;
}

static void rx_text(char *dst, uint32_t max, uint32_t from)   /* the frame's text from byte `from`, cleaned */
{
    uint32_t i;
    for (i = 0; i < max; i++) {
        uint32_t c = from + i < rx_len ? rx_buf[from + i] : 0u;
        dst[i] = c >= 32u && c <= 126u ? (char)c : 0;
        if (!dst[i]) break;
    }
    dst[max] = 0;
}

static void rx_commit(void)
{
    host_seen = 1;
    host_t = fm1_ms;
    if (rx_type == FT_SLOT && rx_len >= 3u) {
        uint32_t s = rx_buf[0];
        if (s < MAXSLOTS) {
            uint8_t st = rx_buf[1] <= S_UNKNOWN ? rx_buf[1] : S_UNKNOWN;
            uint8_t kind = rx_buf[2] & 0x1Fu;
            if (st != slot_state[s]) slot_since[s] = fm1_ms;
            slot_state[s] = st;
            slot_auto[s] = (rx_buf[2] & KIND_AUTO) != 0u;
            slot_loop[s] = (rx_buf[2] & KIND_LOOP) != 0u;
            slot_kind[s] = kind <= 6u ? kind : 0u;
            rx_text(slot_name[s], NAME_CHARS, 3u);
        }
    } else if (rx_type == FT_DETAIL && rx_len >= 1u) {
        if (rx_buf[0] < DETAIL_LINES) rx_text(detail[rx_buf[0]], DETAIL_CHARS, 1u);
    } else if (rx_type == FT_META && rx_len >= 2u) {
        meta_agents = rx_buf[0];
        meta_blocked = rx_buf[1];
    } else if (rx_type == FT_FOOTER && rx_len >= 1u) {
        footer_style = rx_buf[0];
        rx_text(footer, FOOTER_CHARS, 1u);
    } else if (rx_type == FT_BEEP && rx_len >= 1u) {
        beep_id = rx_buf[0];
        beep_seq++;
    } else if (rx_type == FT_SELECT && rx_len >= 1u) {
        if (rx_buf[0] < MAXSLOTS && rx_buf[0] != sel_slot) {   /* the host moves the cursor: no echo */
            sel_slot = rx_buf[0];
            slot_page = (uint8_t)(sel_slot / 27u);
            set.sel = sel_slot; set.slotpage = slot_page;
            set_dirty = 1; set_t = fm1_ms;
        }
    } else if (rx_type == FT_TEXT && rx_len >= 2u) {
        if (rx_buf[0] < MENU_LINES) {
            menu_style[rx_buf[0]] = rx_buf[1];
            rx_text(menu_text[rx_buf[0]], DETAIL_CHARS, 2u);
        }
    } else if (rx_type == FT_PAGE && rx_len >= 1u) {
        if (rx_buf[0] < NPAGES && rx_buf[0] != page) {   /* the host turns the page: no echo */
            page = rx_buf[0];
            set.page = page; set_dirty = 1; set_t = fm1_ms;
        }
    } else if (rx_type == FT_KEYLEDS) {
        uint32_t i;
        kled_on = rx_len != 0u;                 /* an empty frame gives the keys back */
        for (i = 0; i < 27u; i++) kled[i] = i < rx_len ? rx_buf[i] : 0u;
    } else if (rx_type == FT_BTNLEDS) {
        uint32_t i;
        for (i = 0; i < 14u; i++) bled[i] = i < rx_len ? rx_buf[i] : 0u;
    } else if (rx_type == FT_HELLO_ACK) {
        announce_sel = 1;                       /* a host arrived: tell it where the cursor is */
        announce_page = 1;
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
static char *put_age(char *p, uint32_t slot)    /* how long in this state, 3 chars: " 7s" "42m" "13h" */
{
    uint32_t s = (fm1_ms - slot_since[slot]) / 1000u, v;
    char unit;
    if (s < 60u) { v = s; unit = 's'; }
    else if (s < 3600u) { v = s / 60u; unit = 'm'; }
    else { v = s / 3600u; unit = 'h'; if (v > 99u) v = 99u; }
    *p++ = v >= 10u ? (char)('0' + v / 10u) : ' ';
    *p++ = (char)('0' + v % 10u);
    *p++ = unit;
    *p = 0;
    return p;
}

/* ---- the screen */
#define C_ACC RGB(120, 200, 255)
#define C_TXT RGB(190, 190, 190)
#define C_GREY RGB(100, 100, 100)
#define C_RULE RGB(50, 50, 50)
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
    char s[40], *p = s;
    uint32_t i;
    const char *nm = slot_name[slot][0] ? slot_name[slot] : "(empty)";
    *p++ = selected ? '>' : ' ';
    *p++ = ' ';
    put2(p, slot); p += 2;
    *p++ = ' ';
    *p++ = state_glyph(slot_state[slot]);
    *p++ = ' ';
    for (i = 0; i < NAME_CHARS; i++) *p++ = *nm ? *nm++ : ' ';
    if (slot_state[slot] != S_EMPTY) {
        *p++ = ' ';
        p = put_age(p, slot);
        *p++ = ' ';
        *p++ = slot_auto[slot] ? 'A' : ' ';
        *p++ = slot_loop[slot] ? 'R' : ' ';
    }
    *p = 0;
    cv_text(4, y, &FONT_S, s, selected ? C_WHITE : state_colour(slot_state[slot]));
}

static uint16_t style_colour(uint8_t st)
{
    return st == 1u ? C_ACC : st == 2u ? C_GOOD : st == 3u ? C_WARN : st == 4u ? C_BAD :
           st == 5u ? C_WHITE : st == 6u ? C_TXT : C_GREY;
}

static void draw_footer(void)
{
    static const char *const HINT[NPAGES] = {"KEYS PICK - PLAY REC FX ENV", "K1 SCROLLS - REC APPROVES",
                                             "REC APPROVES - GLO+REC ALL", "SELECT MOVES - SEL PICKS"};
    cv_rect(0, 160, 240, 1, C_RULE);
    if (footer[0])
        cv_text(4, 166, &FONT_S, footer, style_colour(footer_style <= 4u ? footer_style : 0u));
    else
        cv_text(4, 166, &FONT_S, HINT[page < NPAGES ? page : 0u], C_GREY);
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
        if (slot_state[sel_slot] != S_EMPTY) {
            p = put_str_r(p, "  ");
            p = put_age(p, sel_slot);
            if (slot_auto[sel_slot]) p = put_str_r(p, " AUTO");
            if (slot_loop[sel_slot]) p = put_str_r(p, " LOOP");
        }
        *p = 0;
        cv_text(4, 18, &FONT_S, s, state_colour(slot_state[sel_slot]));
        for (i = 0; i < DETAIL_SHOWN; i++)
            cv_text(4, 40 + (int32_t)i * 20, &FONT_S, detail[i], C_TXT);
    } else if (page == PAGE_MENU) {             /* the host's page: a title and seven rows */
        if (!menu_text[0][0] && !host_seen) cv_text(4, 0, &FONT_S, "MENU - NEEDS THE HOST", C_GREY);
        cv_text(4, 0, &FONT_S, menu_text[0], style_colour(menu_style[0]));
        for (i = 1; i < MENU_LINES; i++)
            cv_text(4, (int32_t)i * 20, &FONT_S, menu_text[i], style_colour(menu_style[i]));
    } else if (page == PAGE_APPROVALS) {
        cv_text(4, 0, &FONT_S, "BLOCKED - WAITING ON YOU", C_WARN);
        for (i = 0; i < MAXSLOTS && n < 7u; i++)
            if (slot_state[i] == S_BLOCKED) { draw_row(20 + (int32_t)n * 20, i, i == sel_slot); n++; }
        if (!n) cv_text(4, 20, &FONT_S, "(none)", C_GREY);
    } else {                                    /* GRID: this key-page's slots */
        uint32_t base = slot_page * 27u, idx = sel_slot >= base ? sel_slot - base : 0u;
        uint32_t start = idx > GRID_ROWS - 1u ? idx - (GRID_ROWS - 1u) : 0u;
        for (i = 0; i < GRID_ROWS && start + i < 27u; i++)
            draw_row((int32_t)i * 20, base + start + i, base + start + i == sel_slot);
    }
    draw_footer();
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
    if (st == S_DONE) return now - slot_since[slot] < 2000u ? 2u : 1u;
    if (st == S_ERROR) return ((now / 167u) & 1u) ? 2u : 0u;
    return phase500 ? 1u : 0u;                  /* UNKNOWN: glow pulsing at 1 Hz */
}

static void leds(void)
{
    uint32_t k, now = fm1_ms;
    uint32_t chord = kled_on && fm1_in.buttons != 0u;   /* a button is held: the host's lights */
    for (k = 0; k < 27u; k++) {
        uint32_t slot = slot_page * 27u + k;
        uint8_t m = slot < MAXSLOTS ? led_mode(slot, now) : 0u;
        if (chord) m = kled[k] == 3u ? (((now / 125u) & 1u) ? 2u : 0u) : kled[k] <= 2u ? kled[k] : 0u;
        fm1_led_key(KEY_LED(k), m == 2u);
        fm1_led_key_glow(KEY_LED(k), m == 1u);
    }
    fm1_led_key(BTN_PLAY, slot_state[sel_slot] == S_WORKING);
    fm1_led_key(BTN_REC, slot_state[sel_slot] == S_BLOCKED && ((now / 250u) & 1u));
    fm1_led_key(BTN_ARP, slot_auto[sel_slot] != 0u);
    fm1_led_key(BTN_HOME, page == PAGE_GRID);
    fm1_led_key(BTN_EDIT, page == PAGE_DETAIL);
    fm1_led_key(BTN_GLO, page == PAGE_APPROVALS);
    for (k = 0; k < 14u; k++) {                 /* the buttons the host has taken */
        if (!bled[k]) { fm1_led_key_glow(k, 0); continue; }
        fm1_led_key(k, bled[k] == 2u || (bled[k] == 3u && ((now / 250u) & 1u)));
        fm1_led_key_glow(k, bled[k] == 4u);
    }
}

/* ---- the page */
static void page_set(uint32_t p)
{
    if (p >= NPAGES || p == page) return;
    page = (uint8_t)p;
    midi_send(ST_CC, CC_PAGE, page);
    set.page = page; set_dirty = 1; set_t = fm1_ms;
    ui_dirty = 1;
}

/* ---- selection */
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
        if (slot_state[s] != S_EMPTY) {
            sel_set((uint32_t)s);
            slot_page = (uint8_t)(sel_slot / 27u);   /* the keys follow the cursor across pages */
            return;
        }
    }
}

static void app_main(void)
{
    uint32_t prev_notes = 0, t_hb = 0, hello_sent = 0;
    int32_t last_master = -2;
    if (settings_load(&set, sizeof set) == 0 && set.sel < MAXSLOTS &&
        set.page < NPAGES && set.slotpage <= 1u) {
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
            midi_send(ST_CC, CC_HEARTBEAT, PROTO_VER);
        }
        if (announce_sel) {
            announce_sel = 0;
            midi_send(ST_CC, CC_SELECTION, sel_slot);
        }
        if (announce_page) {
            announce_page = 0;
            midi_send(ST_CC, CC_PAGE, page);
        }
        if (host_seen && fm1_ms - host_t > HOST_TIMEOUT_MS) host_seen = 0;   /* the daemon went away */

        /* keys: a press selects the slot and goes out as a note; releases as
         * note offs. With a button held it is a chord: the note alone, the
         * cursor stays (the host knows which button, and what it means). Level-diff like dinghy.c (the host harness stubs
         * fm1_input_note_edges() to 0, and the panel is debounced anyway). */
        keys = fm1_in.notes;
        for (k = 0; k < 27u; k++) {
            if ((keys >> k & 1u) != 0u && (prev_notes >> k & 1u) == 0u) {
                if (!fm1_in.buttons) sel_set(slot_page * 27u + k);
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
        if (b & (1u << BTN_HOME)) page_set(PAGE_GRID);
        if (b & (1u << BTN_GLO)) page_set(PAGE_APPROVALS);
        if (b & (1u << BTN_EDIT)) page_set(PAGE_DETAIL);
        if (b & (1u << BTN_OCT_DOWN) && slot_page > 0u) slot_page--;
        if (b & (1u << BTN_OCT_UP) && slot_page < 1u) slot_page++;
        if (b & ((1u << BTN_OCT_DOWN) | (1u << BTN_OCT_UP))) {
            if (sel_slot / 27u != slot_page) sel_set(slot_page * 27u);
            set.slotpage = slot_page; set_dirty = 1; set_t = fm1_ms;
            ui_dirty = 1;
        }

        /* encoders: out as relative CCs; SELECT and ALGORITHM also act locally
         * (the cursor and the page must not wait for the host) */
        {
            static const uint8_t ENC_ID[7] = {ENC_SELECT, ENC_ALGORITHM, ENC_KNOB1,
                                              ENC_KNOB2, ENC_KNOB3, ENC_KNOB4, ENC_PRESETS};
            uint32_t i;
            for (i = 0; i < 7u; i++) {
                e = fm1_enc_take(ENC_ID[i]);
                if (!e) continue;
                if (e > 63) e = 63;
                if (e < -63) e = -63;
                if (ENC_ID[i] == ENC_SELECT && page != PAGE_MENU) {
                    sel_move(e > 0 ? 1 : -1);   /* the cursor moves here; the host hears CC 120 */
                    continue;                   /* (on MENU, SELECT is the host's: it goes out) */
                }
                midi_send(ST_CC, 85u + i, (uint32_t)(64 + e));
                if (ENC_ID[i] == ENC_ALGORITHM) page_set((page + (e > 0 ? 1u : NPAGES - 1u)) % NPAGES);
            }
        }

        /* MASTER pot: the platform owns the volume (of the chimes); we just
         * report it. (The host test harness has no ADC, so this is device-only.) */
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
#else
        (void)last_master;
#endif

        midi_in();
        leds();

        idle_ok = 1;
        {
            uint32_t header_sig = (uint32_t)meta_agents << 8 | meta_blocked |
                                  (host_seen ? (1u << 16) : 0u) | (uint32_t)page << 20;
            uint32_t phase = (fm1_ms / 250u) & 1u, sec = fm1_ms / 1000u;
            int32_t blink_active = slot_state[sel_slot] == S_BLOCKED || meta_blocked != 0u;
            if (header_sig != drawn_header) { drawn_header = header_sig; draw_header(); }
            if (ui_dirty || (blink_active && phase != drawn_phase) || sec != drawn_sec) {
                ui_dirty = 0;
                drawn_phase = phase;
                drawn_sec = sec;                /* the ages tick once a second */
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
