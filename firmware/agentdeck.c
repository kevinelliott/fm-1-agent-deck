/* SPDX-License-Identifier: GPL-3.0-only */
/* Agent Deck: the FM-1 as a control surface for CLI coding agents.
 *
 * A Dinghy app (platform.c below is Felucca/SLOOP's, via Dinghy — see
 * LICENSING.md in the Dinghy tree). The synth engine is gone; the only
 * sound left is a handful of alert chimes the host asks for. The panel
 * talks to a host daemon (host/agentdeck.py) over USB-MIDI channel 16; the
 * wire format is PROTOCOL.md in this project (this is protocol v6). In short:
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
 *        KEYLEDS, BTNLEDS, GAUGE, HELLO_ACK.
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
 * writes: its menus; SELECT is the host's there), ABOUT (this app, this
 * device, the host: drawn here). OCT-/OCT+ page the 54
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
static const tone_t PAT_WARN[] = {              /* heads up, no hurry: one note, twice */
    {64278423u, 2646, 1161}, {0, 1764, 0}, {64278423u, 2646, 1161}, {0, 0, 0}};
static const tone_t PAT_TICK[] = {{194783100u, 441, 6965}, {0, 0, 0}};   /* feedback: a control moved */
static const tone_t PAT_UP[] = {                /* feedback: sent / approved */
    {85704563u, 1764, 1741}, {114435070u, 1764, 1741}, {0, 0, 0}};
static const tone_t PAT_DOWN[] = {              /* feedback: denied / stopped */
    {114435070u, 1764, 1741}, {85704563u, 1764, 1741}, {0, 0, 0}};
static const tone_t PAT_ALERT[] = {             /* an agent is BLOCKED: three rising calls, thrice */
    {85704563u, 3528, 870}, {114435070u, 3528, 870}, {152709948u, 3528, 870}, {0, 3528, 0},
    {85704563u, 3528, 870}, {114435070u, 3528, 870}, {152709948u, 3528, 870}, {0, 3528, 0},
    {85704563u, 3528, 870}, {114435070u, 3528, 870}, {152709948u, 10584, 290}, {0, 0, 0}};
#define NPATTERNS 9u
static const tone_t *const PATTERNS[NPATTERNS] = {PAT_ATTENTION, PAT_DONE, PAT_ERROR, PAT_ACK, PAT_WARN,
                                                  PAT_TICK, PAT_UP, PAT_DOWN, PAT_ALERT};
static volatile uint32_t beep_seq;
static volatile uint8_t beep_id, beep_gain = 64;   /* gain in 64ths: 64 is as written, 127 twice that */

static void app_audio(int32_t *out, uint32_t n)
{
    static const tone_t *tn;
    static uint32_t seen, ph, inc, env, dec, left, gain = 64;
    uint32_t i;
    if (beep_seq != seen) {
        uint32_t id = beep_id;
        seen = beep_seq;
        tn = id >= 1u && id <= NPATTERNS ? PATTERNS[id - 1u] : 0;
        gain = beep_gain;
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
            v = v * (int32_t)gain / 64;
            out[2u * i] += v;
            out[2u * i + 1u] += v;
            env = env > dec ? env - dec : 0u;
        }
    }
}

/* ---- protocol (PROTOCOL.md) */
#define PROTO_VER 6u
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
#define FT_GAUGE 0x0Bu
#define FT_HELLO_ACK 0x0Fu
#define KIND_AUTO 0x40u                         /* SLOT's kind byte: the host auto-approves this agent */
#define KIND_LOOP 0x20u                         /* ... and: the host repeats a prompt at it */
#define KIND_MARK 0x10u                         /* ... and: it is in the marked group */
#define KIND_QUEUE 0x08u                        /* ... and: the host holds prompts queued for it */
#define GAUGE_NONE 0x7Fu

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
#define PAGE_ABOUT 4u                           /* drawn here: the app, the device, the host */
#define NPAGES 5u
#define SPLASH_MS 900u                          /* how long the power-on splash stays */
#define MENU_LINES 8u                           /* a title and seven rows */

enum { S_EMPTY, S_IDLE, S_WORKING, S_BLOCKED, S_DONE, S_ERROR, S_UNKNOWN };

static uint8_t slot_state[MAXSLOTS];
static uint8_t slot_kind[MAXSLOTS];
static uint8_t slot_auto[MAXSLOTS];
static uint8_t slot_loop[MAXSLOTS];
static uint8_t slot_mark[MAXSLOTS];
static uint8_t slot_queue[MAXSLOTS];
static uint8_t detail_view;                      /* what the host is showing on DETAIL: 0 output, 1 info, 2 diff */
static uint8_t slot_gauge[MAXSLOTS];             /* 0..100: how full its context is; GAUGE_NONE: unknown */
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
static char about_text[2][DETAIL_CHARS + 1u];    /* the host's two lines for ABOUT (TEXT lines 8, 9) */
static uint8_t host_proto;                      /* the protocol version the host said it speaks */
static int32_t master_cc = -1;                  /* MASTER as last sent, 0..127 */
static uint8_t meta_agents, meta_blocked, host_seen, announce_sel;
static uint32_t host_t;                         /* fm1_ms of the host's last frame */
static uint8_t sel_slot, page, slot_page;
static uint32_t ui_dirty = 1, drawn_phase = ~0u, drawn_header = ~0u, drawn_sec = ~0u;

static struct { uint8_t sel, page, slotpage, boxes; } set = {0, 0, 0, 0};   /* boxes: GRID as tiles, not rows */
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
        slot_mark[i] = 0;
        slot_queue[i] = 0;
        slot_gauge[i] = GAUGE_NONE;
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
            uint8_t kind = rx_buf[2] & 0x07u;
            if (st != slot_state[s]) slot_since[s] = fm1_ms;
            slot_state[s] = st;
            slot_auto[s] = (rx_buf[2] & KIND_AUTO) != 0u;
            slot_loop[s] = (rx_buf[2] & KIND_LOOP) != 0u;
            slot_mark[s] = (rx_buf[2] & KIND_MARK) != 0u;
            slot_queue[s] = (rx_buf[2] & KIND_QUEUE) != 0u;
            if (st == S_EMPTY) slot_gauge[s] = GAUGE_NONE;
            slot_kind[s] = kind <= 6u ? kind : 0u;
            rx_text(slot_name[s], NAME_CHARS, 3u);
        }
    } else if (rx_type == FT_DETAIL && rx_len >= 1u) {
        if (rx_buf[0] < DETAIL_LINES) rx_text(detail[rx_buf[0]], DETAIL_CHARS, 1u);
    } else if (rx_type == FT_META && rx_len >= 2u) {
        meta_agents = rx_buf[0];
        meta_blocked = rx_buf[1];
        detail_view = rx_len >= 3u && rx_buf[2] <= 2u ? rx_buf[2] : 0u;   /* v6 */
    } else if (rx_type == FT_FOOTER && rx_len >= 1u) {
        footer_style = rx_buf[0];
        rx_text(footer, FOOTER_CHARS, 1u);
    } else if (rx_type == FT_BEEP && rx_len >= 1u) {
        beep_id = rx_buf[0];
        beep_gain = rx_len >= 2u && rx_buf[1] ? rx_buf[1] : 64u;   /* v5: an optional loudness */
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
        } else if (rx_buf[0] < MENU_LINES + 2u)  /* v6: lines 8 and 9 are for ABOUT */
            rx_text(about_text[rx_buf[0] - MENU_LINES], DETAIL_CHARS, 2u);
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
    } else if (rx_type == FT_GAUGE && rx_len >= 2u) {
        if (rx_buf[0] < MAXSLOTS) slot_gauge[rx_buf[0]] = rx_buf[1] <= 100u ? rx_buf[1] : GAUGE_NONE;
    } else if (rx_type == FT_HELLO_ACK) {
        host_proto = rx_len >= 1u ? rx_buf[0] : 0u;
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

/* ---- the screen. Two canvases, drawn one after the other into the same
 * buffer: the header (240 x 56, top of the LCD) and the page (240 x 184
 * below it). Colour means state everywhere: amber is waiting on you, green
 * is finished, red is broken, white is working, grey is at rest. */
#define C_ACC RGB(120, 200, 255)
#define C_TXT RGB(200, 200, 200)
#define C_GREY RGB(112, 112, 112)
#define C_FAINT RGB(64, 64, 64)
#define C_RULE RGB(50, 50, 50)
#define C_WARN RGB(255, 180, 60)
#define C_BAD RGB(255, 90, 70)
#define C_GOOD RGB(120, 255, 146)
#define C_VIOLET RGB(190, 150, 255)
#define C_SELBG RGB(20, 44, 78)                 /* behind the selected row */
#define C_BODY RGB(26, 30, 40)                  /* the logo's body */

static uint16_t dim4(uint16_t c) { return (uint16_t)((c >> 2) & 0x39E7u); }   /* a quarter as bright: fills behind text */
static uint16_t dim2(uint16_t c) { return (uint16_t)((c >> 1) & 0x7BEFu); }

static uint16_t state_colour(uint8_t s)
{
    return s == S_BLOCKED ? C_WARN : s == S_ERROR ? C_BAD :
           s == S_DONE ? C_GOOD : s == S_WORKING ? C_WHITE : C_GREY;
}
static const char *const KIND_TAG[7] = {"??", "CL", "CX", "OC", "GK", "CP", "SH"};
static uint16_t kind_colour(uint8_t k)
{
    return k == 1u ? RGB(235, 145, 95) : k == 2u ? RGB(90, 205, 175) : k == 3u ? C_VIOLET :
           k == 4u ? RGB(225, 225, 225) : k == 5u ? RGB(120, 170, 255) : C_GREY;
}
static uint16_t gauge_colour(uint32_t pct)
{
    return pct >= 85u ? C_BAD : pct >= 60u ? C_WARN : C_ACC;
}

static void rrect(int32_t x, int32_t y, int32_t w, int32_t h, uint16_t c)   /* a filled box with its corners off */
{
    cv_rect(x + 2, y, w - 4, h, c);
    cv_rect(x + 1, y + 1, w - 2, h - 2, c);
    cv_rect(x, y + 2, w, h - 4, c);
}
static int32_t rtext(int32_t xr, int32_t y, const char *s, uint16_t c)   /* text that ends at xr */
{
    return cv_text(xr - text_w(&FONT_S, s), y, &FONT_S, s, c);
}
static void ctext(int32_t y, const felucca_font_t *f, const char *s, uint16_t c)   /* centred */
{
    cv_text((240 - text_w(f, s)) / 2, y, f, s, c);
}

/* 12 x 12 icons, a row per word, bit 11 on the left */
static const uint16_t ICON_CHECK[12] = {0, 0x003, 0x006, 0x00C, 0x018, 0x830, 0xC60, 0x6C0, 0x380, 0x100, 0, 0};
static const uint16_t ICON_CROSS[12] = {0, 0xC03, 0x606, 0x30C, 0x198, 0x0F0, 0x0F0, 0x198, 0x30C, 0x606, 0xC03, 0};
static const uint16_t ICON_ALERT[12] = {0x060, 0x060, 0x090, 0x090, 0x168, 0x168, 0x264, 0x264, 0x402, 0x462, 0xFFF, 0};
static const uint16_t ICON_PLAY[12] = {0, 0x300, 0x3C0, 0x3F0, 0x3FC, 0x3FF, 0x3FF, 0x3FC, 0x3F0, 0x3C0, 0x300, 0};
static void icon12(int32_t x, int32_t y, const uint16_t *rows, uint16_t c, int32_t scale)
{
    int32_t r, b;
    for (r = 0; r < 12; r++)
        for (b = 0; b < 12; b++)
            if (rows[r] >> (11 - b) & 1u) cv_rect(x + b * scale, y + r * scale, scale, scale, c);
}
static void spinner(int32_t x, int32_t y, uint16_t c)   /* eight dots, a bright one going round */
{
    static const int8_t P[8][2] = {{5, 0}, {8, 2}, {10, 5}, {8, 8}, {5, 10}, {2, 8}, {0, 5}, {2, 2}};
    uint32_t head = (fm1_ms / 250u) & 7u, i;
    for (i = 0; i < 8u; i++) {
        uint32_t age = (head - i) & 7u;
        cv_rect(x + P[i][0], y + P[i][1], 2, 2, age == 0u ? c : age == 1u ? dim2(c) : age == 2u ? dim4(c) : C_FAINT);
    }
}
static void state_icon(int32_t x, int32_t y, uint8_t st, int32_t live)   /* live: it moves (the page); else it holds still (the header) */
{
    uint16_t c = state_colour(st);
    if (st == S_WORKING) { if (live) spinner(x, y, c); else icon12(x, y, ICON_PLAY, c, 1); }
    else if (st == S_BLOCKED) icon12(x, y, ICON_ALERT, live && !((fm1_ms / 250u) & 1u) ? dim2(c) : c, 1);
    else if (st == S_DONE) icon12(x, y, ICON_CHECK, c, 1);
    else if (st == S_ERROR) icon12(x, y, ICON_CROSS, c, 1);
    else if (st == S_IDLE) rrect(x + 3, y + 3, 6, 6, c);
    else if (st == S_UNKNOWN) cv_text(x + 2, y - 2, &FONT_S, "?", c);
    else cv_rect(x + 5, y + 5, 2, 2, C_FAINT);
}

/* the logo: a little deck of nine pads, u pixels to the unit (13u square).
 * live: the pads are the first nine slots, in their state colours. */
static void draw_logo(int32_t x, int32_t y, int32_t u, int32_t live)
{
    static const uint8_t DEMO[9] = {S_DONE, S_WORKING, S_IDLE, S_BLOCKED, S_IDLE, S_WORKING, S_IDLE, S_DONE, S_ERROR};
    uint32_t i;
    rrect(x, y, 13 * u, 13 * u, C_BODY);
    for (i = 0; i < 9u; i++) {
        uint8_t st = live ? slot_state[i] : DEMO[i];
        uint16_t c = st == S_EMPTY ? C_FAINT : st == S_IDLE ? C_ACC : state_colour(st);
        if (u > 2) rrect(x + u + (int32_t)(i % 3u) * 4 * u, y + u + (int32_t)(i / 3u) * 4 * u, 3 * u, 3 * u, c);
        else cv_rect(x + u + (int32_t)(i % 3u) * 4 * u, y + u + (int32_t)(i / 3u) * 4 * u, 3 * u, 3 * u, c);
    }
}

static const char *const PAGE_NAME[NPAGES] = {"GRID", "DETAIL", "WAITING", "MENU", "ABOUT"};

static void draw_header(void)
{
    static const uint8_t ORDER[5] = {S_BLOCKED, S_WORKING, S_DONE, S_ERROR, S_IDLE};
    uint32_t count[7] = {0, 0, 0, 0, 0, 0, 0}, i;
    int32_t x = 4;
    for (i = 0; i < MAXSLOTS; i++) count[slot_state[i]]++;
    count[S_IDLE] += count[S_UNKNOWN];
    cv_begin(240, 56, C_BLACK);
    draw_logo(4, 2, 2, 1);
    cv_text(38, 0, &FONT_S, "AGENT DECK", C_WHITE);
    rtext(236, 0, page == PAGE_GRID && set.boxes ? "BOXES" : PAGE_NAME[page < NPAGES ? page : 0u], C_ACC);
    for (i = 0; i < NPAGES; i++)                /* where we are among the pages */
        cv_rect(38 + (int32_t)i * 14, 21, 11, 3, i == page ? C_ACC : C_RULE);
    if (host_seen) {
        rrect(230, 19, 6, 6, C_GOOD);
        rtext(225, 14, "HOST", C_GREY);
    } else {
        rrect(230, 19, 6, 6, ((fm1_ms / 500u) & 1u) ? C_BAD : dim4(C_BAD));
        rtext(225, 14, "NO HOST", C_BAD);
    }
    for (i = 0; i < 5u; i++) {                  /* a chip per state that has agents in it */
        uint8_t st = ORDER[i];
        char n[3];
        if (!count[st]) continue;
        rrect(x, 34, 42, 19, dim4(state_colour(st)));
        state_icon(x + 5, 37, st, 0);
        n[0] = count[st] >= 10u ? (char)('0' + count[st] / 10u % 10u) : (char)('0' + count[st]);
        n[1] = count[st] >= 10u ? (char)('0' + count[st] % 10u) : 0;
        n[2] = 0;
        cv_text(x + 22, 35, &FONT_S, n, state_colour(st));
        x += 46;
    }
    if (x == 4) cv_text(4, 35, &FONT_S, host_seen ? "no agents" : "waiting for the host", C_GREY);
    cv_blit(0, 0);
}

static void draw_flags(int32_t x, int32_t y, uint32_t slot)   /* A auto-approve, R repeat, + marked, Q queue */
{
    if (slot_auto[slot]) cv_text(x, y, &FONT_S, "A", C_WARN);
    if (slot_loop[slot]) cv_text(x + 9, y, &FONT_S, "R", C_ACC);
    if (slot_mark[slot]) cv_text(x + 18, y, &FONT_S, "+", C_WHITE);
    if (slot_queue[slot]) cv_text(x + 27, y, &FONT_S, "Q", C_VIOLET);
}

static void draw_row(int32_t y, uint32_t slot, int32_t selected)
{
    char s[8];
    uint8_t st = slot_state[slot];
    uint16_t bg = selected ? C_SELBG : C_BLACK;
    if (selected) cv_rect(0, y, 240, 19, C_SELBG);
    cv_rect(0, y, 3, 19, st == S_EMPTY ? (selected ? C_ACC : C_RULE) : state_colour(st));
    put2(s, slot); s[2] = 0;
    cv_text(7, y + 2, &FONT_S, s, selected ? C_WHITE : st == S_EMPTY ? C_FAINT : C_GREY);
    state_icon(27, y + 3, st, 1);
    if (st == S_EMPTY) {
        if (selected) cv_text(44, y + 2, &FONT_S, "empty - PLAY starts one", C_GREY);
        return;
    }
    cv_text(44, y + 2, &FONT_S, slot_name[slot], selected ? C_WHITE : st == S_IDLE ? C_TXT : state_colour(st));
    cv_rect(142, y, 98, 19, bg);                /* a long name stops here */
    cv_text(144, y + 2, &FONT_S, KIND_TAG[slot_kind[slot] <= 6u ? slot_kind[slot] : 0u], kind_colour(slot_kind[slot]));
    put_age(s, slot);
    rtext(190, y + 2, s[0] == ' ' ? s + 1 : s, C_GREY);
    draw_flags(194, y + 2, slot);
    if (slot_gauge[slot] <= 100u) {             /* context: a bar that fills upward */
        int32_t h = (int32_t)(slot_gauge[slot] * 15u + 99u) / 100;
        cv_rect(232, y + 2, 5, 15, C_RULE);
        if (h > 0) cv_rect(232, y + 17 - h, 5, h, gauge_colour(slot_gauge[slot]));
    }
}

static uint16_t style_colour(uint8_t st)
{
    return st == 1u ? C_ACC : st == 2u ? C_GOOD : st == 3u ? C_WARN : st == 4u ? C_BAD :
           st == 5u ? C_WHITE : st == 6u ? C_TXT : C_GREY;
}

static void draw_footer(void)
{
    static const char *const HINT[NPAGES] = {"KEYS PICK - PLAY REC FX ENV", "K1 SCROLLS - EDIT: VIEW",
                                             "REC APPROVES - GLO+REC ALL", "SELECT MOVES - SEL PICKS",
                                             "GPL-3.0 - BUILT ON DINGHY"};
    cv_rect(0, 160, 240, 1, C_RULE);
    if (footer[0]) {
        uint16_t c = style_colour(footer_style <= 4u ? footer_style : 0u);
        if (footer_style >= 1u && footer_style <= 4u) {   /* a message: a tinted band with a bright edge */
            cv_rect(0, 162, 240, 22, dim4(c));
            cv_rect(0, 162, 3, 22, c);
        }
        cv_text(8, 166, &FONT_S, footer, c);
    } else
        cv_text(8, 166, &FONT_S, HINT[page < NPAGES ? page : 0u], C_GREY);
}

static void draw_detail_line(int32_t y, const char *s)   /* the host's text, coloured by what it looks like */
{
    uint32_t i;
    if (!s[0]) return;
    if (s[0] == '+' || (s[0] == 'n' && s[1] == 'e' && s[2] == 'w' && s[3] == ':')) { cv_text(4, y, &FONT_S, s, C_GOOD); return; }
    if (s[0] == '>') { cv_text(4, y, &FONT_S, s, C_WHITE); return; }
    if (s[0] == '(' || (s[0] == '-' && s[1] == '-')) { cv_text(4, y, &FONT_S, s, C_GREY); return; }
    for (i = 0; i < 12u && s[i]; i++) {         /* LABEL: the rest */
        char ch = s[i];
        if (ch == ':' && i >= 2u) {
            char pre[14];
            uint32_t j;
            for (j = 0; j <= i; j++) pre[j] = s[j];
            pre[i + 1u] = 0;
            cv_text(cv_text(4, y, &FONT_S, pre, C_ACC), y, &FONT_S, s + i + 1u, C_TXT);
            return;
        }
        if (!((ch >= 'A' && ch <= 'Z') || ch == ' ' || ch == '+' || ch == '-' || (ch >= '0' && ch <= '9'))) break;
    }
    cv_text(4, y, &FONT_S, s, C_TXT);
}

static void draw_detail(void)
{
    uint8_t st = slot_state[sel_slot], kind = slot_kind[sel_slot] <= 6u ? slot_kind[sel_slot] : 0u;
    uint16_t col = state_colour(st);
    char s[8];
    int32_t x, w;
    uint32_t i;
    if (st == S_EMPTY) {
        put2(s, sel_slot); s[2] = 0;
        cv_text(cv_text(4, 0, &FONT_S, "SLOT ", C_GREY), 0, &FONT_S, s, C_WHITE);
        ctext(60, &FONT_S, "nothing runs here yet", C_GREY);
        ctext(82, &FONT_S, "PLAY starts an agent", C_ACC);
        return;
    }
    cv_text(4, 0, &FONT_S, slot_name[sel_slot], C_WHITE);
    if (detail_view) {                          /* which of the host's views this is */
        const char *tag = detail_view == 1u ? "INFO" : "DIFF";
        w = text_w(&FONT_S, tag) + 10;
        rrect(236 - w, 0, w, 17, dim4(C_ACC));
        cv_text(241 - w, 0, &FONT_S, tag, C_ACC);
    }
    w = text_w(&FONT_S, STATE_NAME[st]) + 26;   /* the state, as a pill */
    rrect(4, 19, w, 18, dim4(col));
    state_icon(8, 22, st, 1);
    cv_text(24, 20, &FONT_S, STATE_NAME[st], col);
    put_age(s, sel_slot);
    x = cv_text(4 + w + 6, 20, &FONT_S, s[0] == ' ' ? s + 1 : s, C_GREY);
    draw_flags(x + 6, 20, sel_slot);
    rtext(236, 20, KIND_NAME[kind], kind_colour(kind));
    if (slot_gauge[sel_slot] <= 100u) {
        cv_rect(4, 39, 232, 3, C_RULE);
        cv_rect(4, 39, (int32_t)(slot_gauge[sel_slot] * 232u / 100u), 3, gauge_colour(slot_gauge[sel_slot]));
    }
    for (i = 0; i < DETAIL_SHOWN; i++) draw_detail_line(45 + (int32_t)i * 19, detail[i]);
}

/* GRID as boxes: a tile per slot, filled in its state's colour. The grid
 * grows with the deck: four big tiles for a few agents, down to all 27
 * keys at once. */
static void fit_text(char *dst, const char *src, int32_t maxw)   /* as much of src as fits in maxw pixels */
{
    uint32_t i = 0;
    dst[0] = 0;
    while (src[i] && i < NAME_CHARS) {
        dst[i] = src[i];
        dst[i + 1u] = 0;
        if (text_w(&FONT_S, dst) > maxw) { dst[i] = 0; break; }
        i++;
    }
}

static void draw_boxes(void)
{
    static const uint8_t LAY[7][3] = {{4, 2, 2}, {6, 3, 2}, {9, 3, 3}, {12, 4, 3}, {16, 4, 4}, {20, 5, 4}, {27, 7, 4}};
    uint32_t base = slot_page * 27u, n = 4, i, l = 0, cols, rows;
    int32_t tw, th;
    for (i = 0; i < 27u; i++)
        if ((slot_state[base + i] != S_EMPTY || base + i == sel_slot) && i + 1u > n) n = i + 1u;
    while (LAY[l][0] < n) l++;
    cols = LAY[l][1]; rows = LAY[l][2];
    tw = 240 / (int32_t)cols; th = 160 / (int32_t)rows;
    for (i = 0; i < cols * rows && i < 27u; i++) {
        uint32_t slot = base + i;
        int32_t x = (int32_t)(i % cols) * tw + 1, y = (int32_t)(i / cols) * th + 1, w = tw - 2, h = th - 2;
        uint8_t st = slot_state[slot];
        uint16_t col = state_colour(st), fill;
        int32_t sel = slot == sel_slot;
        char s[NAME_CHARS + 2u];
        put2(s, slot); s[2] = 0;
        if (st == S_EMPTY) {                    /* an outline: the key is free */
            rrect(x, y, w, h, sel ? C_ACC : C_RULE);
            rrect(x + 1, y + 1, w - 2, h - 2, C_BLACK);
            cv_text(x + (w - text_w(&FONT_S, s)) / 2, y + (h - 16) / 2, &FONT_S, s, sel ? C_ACC : C_FAINT);
            continue;
        }
        fill = st == S_IDLE || st == S_UNKNOWN ? C_BODY : st == S_WORKING ? RGB(52, 56, 66) :
               st == S_BLOCKED && ((fm1_ms / 250u) & 1u) ? dim2(col) : dim4(col);
        rrect(x, y, w, h, sel ? C_WHITE : st == S_IDLE ? C_FAINT : dim2(col));
        rrect(x + 2, y + 2, w - 4, h - 4, fill);
        if (w < 40) {                           /* the smallest tiles: number and icon */
            cv_text(x + (w - text_w(&FONT_S, s)) / 2, y + 3, &FONT_S, s, sel ? C_WHITE : C_TXT);
            state_icon(x + (w - 12) / 2, y + 21, st, 1);
        } else {
            cv_text(x + 6, y + 3, &FONT_S, s, sel ? C_WHITE : C_GREY);
            state_icon(x + w - 21, y + 5, st, 1);
            fit_text(s, slot_name[slot], w - 14);
            cv_text(x + (w - text_w(&FONT_S, s)) / 2, y + (h >= 70 ? 28 : 19), &FONT_S, s,
                    st == S_IDLE ? C_TXT : col);
            if (h >= 50) {                      /* room for a bottom line: kind, flags, age */
                int32_t by = y + (h >= 70 ? 52 : 34);
                cv_text(x + 6, by, &FONT_S, KIND_TAG[slot_kind[slot] <= 6u ? slot_kind[slot] : 0u],
                        kind_colour(slot_kind[slot]));
                if (w >= 110) draw_flags(x + 30, by, slot);
                put_age(s, slot);
                rtext(x + w - 9, by, s[0] == ' ' ? s + 1 : s, C_GREY);
            }
        }
        if (slot_gauge[slot] <= 100u) {         /* context: up the right edge, as on a row */
            int32_t gh = h - 8, f = ((int32_t)slot_gauge[slot] * gh + 99) / 100;
            cv_rect(x + w - 6, y + 4, 3, gh, C_RULE);
            if (f > 0) cv_rect(x + w - 6, y + 4 + gh - f, 3, f, gauge_colour(slot_gauge[slot]));
        }
    }
}

static void put_num(char **pp, uint32_t v)      /* decimal, no padding */
{
    char t[10];
    uint32_t n = 0;
    do { t[n++] = (char)('0' + v % 10u); v /= 10u; } while (v && n < sizeof t);
    while (n) *(*pp)++ = t[--n];
    **pp = 0;
}

#ifndef FELUCCA_ID
#define FELUCCA_ID "FM-1"                       /* the package identity (tools/build.py); none on the host harness */
#endif
static void about_row(int32_t y, const char *label, const char *value, uint16_t c)
{
    cv_text(8, y, &FONT_S, label, C_GREY);
    cv_text(84, y, &FONT_S, value, c);
}

static void draw_about(void)
{
    char s[40], *p;
    uint32_t t = fm1_ms / 1000u, n = 0, i;
    for (i = 0; i < MAXSLOTS; i++) n += slot_state[i] != S_EMPTY;
    draw_logo(8, 4, 4, 1);
    cv_text(70, 4, &FONT_S, "AGENT DECK", C_WHITE);
    cv_text(70, 22, &FONT_S, "firmware " APP_VER, C_ACC);
    p = put_str_r(s, "protocol v"); put_num(&p, PROTO_VER);
    cv_text(70, 40, &FONT_S, s, C_GREY);
    about_row(62, "DEVICE", FELUCCA_ID, C_TXT);
    if (host_seen) {
        p = put_str_r(s, "connected");
        if (host_proto) { p = put_str_r(p, ", v"); put_num(&p, host_proto); }
        about_row(78, "HOST", s, C_GOOD);
    } else
        about_row(78, "HOST", "not connected", C_BAD);
    about_row(94, "DAEMON", about_text[0][0] ? about_text[0] : "-", C_TXT);
    p = s; put_num(&p, n); p = put_str_r(p, " in "); put_num(&p, MAXSLOTS); put_str_r(p, " slots");
    about_row(110, "AGENTS", s, C_TXT);
    p = s; put_num(&p, t / 3600u); *p++ = ':'; put2(p, t / 60u % 60u); p += 2; *p++ = ':'; put2(p, t % 60u); p[2] = 0;
    about_row(126, "UPTIME", s, C_TXT);
    if (about_text[1][0]) about_row(142, "SYSTEM", about_text[1], C_TXT);
    else if (master_cc >= 0) {
        p = put_str_r(s, "volume "); put_num(&p, (uint32_t)master_cc * 100u / 127u); put_str_r(p, "%");
        about_row(142, "SOUND", s, C_TXT);
    }
}

static void draw_content(void)
{
    uint32_t i, n = 0;
    cv_begin(240, 184, C_BLACK);
    if (page == PAGE_DETAIL) {
        draw_detail();
    } else if (page == PAGE_ABOUT) {
        draw_about();
    } else if (page == PAGE_MENU) {             /* the host's page: a title and seven rows */
        cv_rect(0, 0, 240, 19, dim4(C_ACC));
        cv_rect(0, 0, 3, 19, C_ACC);
        cv_text(8, 1, &FONT_S, menu_text[0][0] ? menu_text[0] : host_seen ? "MENU" : "MENU - NEEDS THE HOST", C_WHITE);
        for (i = 1; i < MENU_LINES; i++) {
            if (menu_style[i] == 5u) {          /* the cursor's row */
                cv_rect(0, (int32_t)i * 20, 240, 19, C_SELBG);
                cv_rect(0, (int32_t)i * 20, 3, 19, C_ACC);
            }
            cv_text(8, (int32_t)i * 20 + 2, &FONT_S, menu_text[i], style_colour(menu_style[i]));
        }
    } else if (page == PAGE_APPROVALS) {
        for (i = 0; i < MAXSLOTS; i++) n += slot_state[i] == S_BLOCKED;
        if (n) {
            cv_rect(0, 0, 240, 19, dim4(C_WARN));
            cv_rect(0, 0, 3, 19, C_WARN);
            cv_text(8, 1, &FONT_S, "WAITING ON YOU", C_WARN);
            for (i = 0, n = 0; i < MAXSLOTS && n < 7u; i++)
                if (slot_state[i] == S_BLOCKED) { draw_row(20 + (int32_t)n * 20, i, i == sel_slot); n++; }
        } else {                                /* nobody: a big tick */
            icon12(96, 22, ICON_CHECK, C_GOOD, 4);
            ctext(84, &FONT_S, "ALL CLEAR", C_GOOD);
            ctext(106, &FONT_S, "nobody is waiting on you", C_GREY);
        }
    } else {                                    /* GRID: this key-page's slots */
        uint32_t base = slot_page * 27u, idx = sel_slot >= base ? sel_slot - base : 0u;
        uint32_t start = idx > GRID_ROWS - 1u ? idx - (GRID_ROWS - 1u) : 0u;
        for (i = 0; i < MAXSLOTS; i++) n += slot_state[i] != S_EMPTY;
        if (!n) {                               /* an empty deck: say what to do */
            draw_logo(88, 8, 5, 0);
            if (host_seen) {
                ctext(84, &FONT_S, "NO AGENTS YET", C_WHITE);
                ctext(106, &FONT_S, "pick a key, press PLAY", C_ACC);
                ctext(126, &FONT_S, "or start one in a terminal", C_GREY);
            } else {
                static const char *const DOTS[4] = {"", ".", "..", "..."};
                char s[32];
                put_str_r(put_str_r(s, "WAITING FOR THE HOST"), DOTS[(fm1_ms / 500u) & 3u]);
                cv_text((240 - text_w(&FONT_S, "WAITING FOR THE HOST")) / 2, 84, &FONT_S, s, C_WARN);
                ctext(106, &FONT_S, "run host/agentdeck.py", C_ACC);
                ctext(126, &FONT_S, "on the computer", C_GREY);
            }
        } else if (set.boxes)
            draw_boxes();
        else
            for (i = 0; i < GRID_ROWS && start + i < 27u; i++)
                draw_row((int32_t)i * 20, base + start + i, base + start + i == sel_slot);
    }
    draw_footer();
    cv_blit(0, 56);
}

static void draw_splash(void)                   /* at power-on, for a moment */
{
    cv_begin(240, 184, C_BLACK);
    draw_logo(81, 14, 6, 0);
    ctext(104, &FONT_L, "AGENT DECK", C_WHITE);
    ctext(144, &FONT_S, "firmware " APP_VER, C_ACC);
    ctext(164, &FONT_S, "a control surface for agents", C_GREY);
    cv_blit(0, 28);
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
        set.page < NPAGES && set.slotpage <= 1u && set.boxes <= 1u) {
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
        if (b & (1u << BTN_HOME)) {
            if (page == PAGE_GRID) {            /* HOME again: the list, or the boxes */
                set.boxes = !set.boxes;
                set_dirty = 1; set_t = fm1_ms;
                drawn_header = ~0u;
                ui_dirty = 1;
            } else
                page_set(PAGE_GRID);
        }
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
                    master_cc = cc;
                    ui_dirty = 1;
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
        if (fm1_ms < SPLASH_MS) {               /* power-on: the splash, once */
            static uint32_t shown;
            if (!shown) { shown = 1; draw_splash(); }
        } else {
            uint32_t header_sig = (uint32_t)page | (host_seen ? 8u : ((fm1_ms / 500u) & 1u) << 4), moving = 0, i;
            uint32_t phase = (fm1_ms / 250u) & 1u, sec = fm1_ms / 1000u;
            for (i = 0; i < MAXSLOTS; i++) {
                header_sig = header_sig * 33u + slot_state[i];
                moving |= slot_state[i] == S_WORKING || slot_state[i] == S_BLOCKED;
            }
            if (!host_seen && meta_agents == 0u) moving = 1;   /* the waiting dots */
            if (header_sig != drawn_header) { drawn_header = header_sig; draw_header(); }
            if (ui_dirty || (moving && phase != drawn_phase) || sec != drawn_sec) {
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
