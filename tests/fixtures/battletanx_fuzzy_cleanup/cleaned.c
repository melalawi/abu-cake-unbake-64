#include "span_1000/code_8010527C.h"
#include "audio_callbacks.h"
#include "types.h"
#include "bt_gbi.h"
#include "common/draft_fields_func_80106010_us.h"



extern s32 D_803AAD88;
extern s32 D_803AAD8C;
extern f32 D_803AAD90;
extern f32 D_803AAD94;
extern void *D_803AAD98;
extern s32 D_803B75A0;
extern u8 D_803B75A4;
extern u8 D_803B75A5;
extern u8 D_803B75A6;
extern u8 D_803B75A7;
extern u8 D_803B75A8;

/* The canonical entry transports the display-list head through one s32 slot.
 * Every Gfx command is eight bytes; the font field views remain producer-owned. */
s32 func_80106010_us(s32 *head, s32 character)
{
    u8 glyph;
    u8 glyph_s;
    u8 glyph_t;
    u8 glyph_span;
    void *glyph_record;
    s32 image_height;
    s32 image_width;
    void *pixels;
    Gfx *dl;
    s32 line;
    u32 texture_s;
    u32 texture_t;
    u32 dsdx;
    u32 dtdy;
    s32 advance;

    glyph = ((struct Measured_func_80106010_us_586592ff79c1 *)(D_803AAD98 + character))->value;
    if (glyph == 0xFF) return 0xFF;
    dl = (Gfx *)*head;
    image_height = ((struct Measured_func_80106010_us_5bc2a4c5e17f *)D_803AAD98)->value;
    image_width = ((struct Measured_func_80106010_us_7cc74cb35a88 *)D_803AAD98)->value;
    glyph_record = D_803AAD98 + glyph * 4;
    glyph_s = ((struct Measured_func_80106010_us_6118e4b73d6f *)glyph_record)->value;
    glyph_t = ((struct Measured_func_80106010_us_86ecf9630c4a *)glyph_record)->value;
    glyph_span = ((struct Measured_func_80106010_us_78c30cba5e02 *)glyph_record)->value;
    pixels = D_803AAD98 + ((struct Measured_func_80106010_us_f88949686c7b *)D_803AAD98)->value;
    line = ((u32)(glyph_span + 8)) >> 3;

    gDPSetPrimColor(dl++, 0, 0, D_803B75A4, D_803B75A5, D_803B75A6, D_803B75A7);
    gDPSetTextureImage(dl++, G_IM_FMT_IA, G_IM_SIZ_8b, image_width, pixels);
    gDPSetTile(dl++, G_IM_FMT_IA, G_IM_SIZ_8b, line, 0, G_TX_LOADTILE, 0,
               G_TX_CLAMP, G_TX_NOMASK, G_TX_NOLOD, G_TX_CLAMP, G_TX_NOMASK, G_TX_NOLOD);
    gDPLoadSync(dl++);
    gDPLoadTile(dl++, G_TX_LOADTILE, glyph_s * 4, glyph_t * 4,
                (glyph_s + glyph_span) * 4, (glyph_t + image_height) * 4);
    gDPPipeSync(dl++);
    gDPSetTile(dl++, G_IM_FMT_IA, G_IM_SIZ_8b, line, 0, G_TX_RENDERTILE, 0,
               G_TX_CLAMP, G_TX_NOMASK, G_TX_NOLOD, G_TX_CLAMP, G_TX_NOMASK, G_TX_NOLOD);
    gDPSetTileSize(dl++, G_TX_RENDERTILE, glyph_s * 4, glyph_t * 4,
                   (glyph_s + glyph_span) * 4, (glyph_t + image_height) * 4);
    texture_s = (u32)((f64)glyph_s * 32.0);
    texture_t = (u32)((f64)glyph_t * 32.0);
    dsdx = (u32)(1024.0f / D_803AAD90);
    dtdy = (u32)(1024.0f / D_803AAD94);

    gSPTextureRectangle(dl, D_803AAD88 * 4, D_803AAD8C * 4,
        (D_803AAD88 + (s32)((f32)glyph_span * D_803AAD90)) * 4,
        (D_803AAD8C + (s32)((f32)image_height * D_803AAD94)) * 4,
        G_TX_RENDERTILE, texture_s, texture_t, dsdx, dtdy);
    dl += 3;
    advance = (s32)((f32)(glyph_span * D_803B75A0) * D_803AAD90);
    D_803AAD88 += advance;
    *head = (s32)dl;
    return advance;
}
