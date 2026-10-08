#include "menu_rom_records.h"
#include "menu_rom_code_addresses.h"
#include "common/unused.h"
#include "span_C76B0/data.h"

/* Existing paged list address label; declaration preserved from its consumers. */
extern char D_0044E468[];

/* US-rev1 ROM1BCF94..1BEE48, compiled at resident8015414C.
 * Stored addresses retain the paged004xxxxx representation; no callable
 * C ABI or backing object is invented for a code-address label.
 * Ownership splits the descriptor at virtual00450E3C into prefix/tail.
 * The final debug-name prefix continues outside the second claim. */
struct resident_menu_pak_options {
    s16 preceding_item_kind, preceding_item_index;
    MenuRomDescriptor entries_44EF98[8];
    MenuRomList lists_44F0B8[5];
    MenuRomDescriptor entries_44F16C[10];
    MenuRomList lists_44F2D4[2];
    MenuRomDescriptor entries_44F31C[9];
    MenuRomList lists_44F460[3];
    MenuRomDescriptor entries_44F4CC[8];
    MenuRomList lists_44F5EC[17];
    MenuRomDescriptor entries_44F850[8];
    MenuRomList lists_44F970[2];
    MenuRomDescriptor entries_44F9B8[8];
    MenuRomList lists_44FAD8[5];
    u8 cheat_navigation[4];
    MenuRomCheat cheats[12];
    MenuRomDescriptor entries_44FC50[33];
    MenuRomList lists_4500F4[1];
    MenuRomDescriptor entries_450118[20];
    MenuRomList lists_4503E8[1];
    u32 menu_field_45040C;
    MenuRomDescriptor entries_450410[3];
    MenuRomList lists_45047C[1];
    MenuRomDescriptor entries_4504A0[8];
    MenuRomList lists_4505C0[1];
    MenuRomDescriptor entries_4505E4[5];
    MenuRomList lists_450698[1];
    s32 preview_resource;
    s32 preview_kind;
    f32 preview_position[3];
    f32 preview_depth;
    f32 preview_scale[3];
    s32 preview_width;
    s32 preview_height;
    f32 preview_angle_step;
    MenuRomDescriptor entries_4506EC[3];
    MenuRomList lists_450758[2];
    MenuRomDescriptor entries_4507A0[4];
    MenuRomList lists_450830[1];
    f32 selection_scale_offsets[16];
    MenuRomDescriptor entries_450894[11];
    MenuRomList lists_450A20[1];
    MenuRomDescriptor entries_450A44[4];
    MenuRomList lists_450AD4[1];
    MenuRomDescriptor entries_450AF8[5];
    MenuRomList lists_450BAC[2];
    MenuRomDescriptor entries_450BF4[1];
    MenuRomList lists_450C18[1];
    MenuRomDescriptor entries_450C3C[7];
    MenuRomList lists_450D38[1];
    u32 menu_field_450D5C;
    MenuRomDescriptor entries_450D60[2];
    MenuRomList lists_450DA8[1];
    s16 inventory_selectors[2];
    MenuRomDescriptor entries_450DD0[3];
    MenuRomDescriptorPrefix inventory_entry_3_prefix;
};
const struct resident_menu_pak_options resident_menu_pak_options = {
    0, 0,
    {
        {0, 0, 0x00011001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D36E4, 0x30U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80407F6C_de}, {&rw_menu_paged_func_80407748_de}, 0, 0},
        {0, 0, 0x00011001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D36E4, 0x34U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80407F6C_de}, {&rw_menu_paged_func_80407748_de}, 0, 0},
        {0, 0, 0x00011001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D36E4, 0x38U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80407F6C_de}, {&rw_menu_paged_func_80407748_de}, 0, 0},
        {0, 0, 0x00011001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D36E4, 0x3CU), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80407F6C_de}, {&rw_menu_paged_func_80407748_de}, 0, 0},
        {0, 0, 0x00011001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D36E4, 0x40U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80407F6C_de}, {&rw_menu_paged_func_80407748_de}, 0, 0},
        {0, 0, 0x00011001U, 0, 3, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80408118_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 10, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040A244_de}, {&rw_menu_paged_func_804082FC_de}, 0, 0},
        {0, 0, 0x00010041U, 0, 4, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3748, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040A458_de}, {&rw_menu_paged_func_804085E0_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 - 0x318U), 22, 0, {&rw_menu_paged_func_804066BC_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_80409C0C_de}, 21, 0, 0, 0, 0},
        {((u32)&D_0044E468 - 0x318U), 22, 0, {&rw_menu_paged_func_80406434_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_80409C0C_de}, 20, 0, 0, 0, 0},
        {((u32)&D_0044E468 - 0x318U), 22, 0, {&rw_menu_paged_func_804070F4_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_80409CEC_de}, 21, 0, 0, 0, 0},
        {((u32)&D_0044E468 - 0x318U), 22, 0, {&rw_menu_paged_func_80406F7C_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_80409CEC_de}, 20, 0, 0, 0, 0},
        {((u32)&D_0044E468 - 0x318U), 22, 0, {&rw_menu_paged_func_804069F4_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_80409E10_de}, 20, 0, 0, 0, 0}
    },
    {
        {1, 0, 0x90000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x08000801U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D37A0, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040A4A0_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 20, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040A5E8_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 5, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D37B0, 0xCU), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040A614_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 10, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040A4D4_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D37B0, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040A530_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D37B0, 0x8U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040A58C_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 20, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D37E4, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040A6A8_de}, {0}, 0, 0},
        {0, 0, 0x00010040U, 125, 10, {0, 1, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0xCU), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80408870_de}, 0, 0},
        {0, 0, 0x00024040U, 10, 0, {255, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0x10U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8040A6DC_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0xB4U), 10, 0, {&rw_menu_paged_func_8040A47C_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 8, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0xB4U), 10, 0, {&rw_menu_paged_func_8040A490_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 9, 0, 0, 0, 0}
    },
    {
        {1, 0, 0x90000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x08000021U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D37C0, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040A7BC_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 50, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040A83C_us}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 2, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2480, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040A928_us}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 2, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2480, 0x8U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040A9A0_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 2, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2480, 0xCU), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80408AC4_us}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 16, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D37E4, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040A9F8_de}, {0}, 0, 0},
        {0, 0, 0x00010040U, 125, 10, {0, 1, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0xCU), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80408C4C_de}, 0, 0},
        {0, 0, 0x00024040U, 10, 0, {255, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0x10U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8040AA4C_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x264U), 9, 0, {&rw_menu_paged_func_8040A748_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x264U), 9, 0, {&rw_menu_paged_func_8040A764_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 8, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x264U), 9, 0, {&rw_menu_paged_func_8040A77C_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 8, 0, 0, 0, 0}
    },
    {
        {1, 0, 0x90000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x08000021U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040AC54_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 30, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040ACA0_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 5, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040ADCC_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 5, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040AEF8_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 5, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040B024_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 5, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040B150_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 25, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D37F8, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040B27C_de}, {&rw_menu_paged_func_80408DF0_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040AAB8_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040AAC8_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040AADC_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040AAF0_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040AB04_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040AB18_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040AB2C_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040AB40_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040AB54_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040ABA0_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040ABBC_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040ABDC_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040ABF0_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040AC04_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040AC40_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040AC18_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x414U), 8, 0, {&rw_menu_paged_func_8040AC2C_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 7, 0, 0, 0, 0}
    },
    {
        {1, 0, 0x90000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x08000021U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D393C, 0x4U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 40, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040B3DC_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040B410_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040B444_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 20, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3958, 0x4U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8040B4E0_de}, 0, 0},
        {0, 0, 0x00010041U, 0, 5, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3958, 0x8U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8040B58C_de}, 0, 0},
        {0, 0, 0x00010041U, 0, 5, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040B6CC_de}, {&rw_menu_paged_func_8040B648_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x798U), 8, 0, {&rw_menu_paged_func_8040B3A8_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_8040B2C8_de}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x798U), 8, 0, {&rw_menu_paged_func_8040B3C4_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_8040B2C8_de}, 5, 0, 0, 0, 0}
    },
    {
        {1, 0, 0x90000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x08000801U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040B868_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 35, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040B8E4_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 4, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040B960_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 4, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040B9DC_de}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 4, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040BA58_de}, {0}, 0, 0},
        {0, 0, 0x00012001U, 0, 35, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D39DC, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040BAD4_de}, {&rw_menu_paged_func_80409144_de}, 0, 0},
        {0, 0, 0x00012001U, 0, 5, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800DE7D4_eu_x, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8040BBC0_us}, {&rw_menu_paged_func_80409448_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x900U), 8, 0, {&rw_menu_paged_func_8040B7E0_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_8040B700_de}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x900U), 8, 0, {&rw_menu_paged_func_8040B7F8_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_8040B700_de}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x900U), 8, 0, {&rw_menu_paged_func_8040B814_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_8040B700_de}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x900U), 8, 0, {&rw_menu_paged_func_8040B830_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_8040B700_de}, 7, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x900U), 8, 0, {&rw_menu_paged_func_8040B84C_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_8040B700_de}, 7, 0, 0, 0, 0}
    },
    {0, 1, 2, 1},
    {
        {RW_MENU_RESIDENT_ADDRESS(D_800DE048_de, 0x134U), RW_MENU_RESIDENT_ADDRESS(D_800D3A9C, 0x18U), 0x00000020U, 1},
        {RW_MENU_RESIDENT_ADDRESS(D_800DE048_de, 0x124U), RW_MENU_RESIDENT_ADDRESS(D_800D3A9C, 0x20U), 0x00000080U, 1},
        {RW_MENU_RESIDENT_ADDRESS(D_800DE048_de, 0x11CU), RW_MENU_RESIDENT_ADDRESS(D_800D3A9C, 0x24U), 0x00000100U, 1},
        {RW_MENU_RESIDENT_ADDRESS(D_800DE048_de, 0x110U), RW_MENU_RESIDENT_ADDRESS(D_800D3A9C, 0x1CU), 0x00000040U, 1},
        {RW_MENU_RESIDENT_ADDRESS(D_800DE048_de, 0x104U), RW_MENU_RESIDENT_ADDRESS(D_800D3A9C, 0x28U), 0x00000200U, 1},
        {RW_MENU_RESIDENT_ADDRESS(D_800DE048_de, 0xF0U), RW_MENU_RESIDENT_ADDRESS(D_800D3A9C, 0x2CU), 0x00000400U, 1},
        {RW_MENU_RESIDENT_ADDRESS(D_800DE048_de, 0xE4U), RW_MENU_RESIDENT_ADDRESS(D_800D3A9C, 0x30U), 0x00000800U, 1},
        {RW_MENU_RESIDENT_ADDRESS(D_800DE048_de, 0xDCU), RW_MENU_RESIDENT_ADDRESS(D_800D3A9C, 0x34U), 0x00001000U, 1},
        {RW_MENU_RESIDENT_ADDRESS(D_800DE048_de, 0xD0U), RW_MENU_RESIDENT_ADDRESS(D_800D3A9C, 0x3CU), 0x00004000U, 1},
        {RW_MENU_RESIDENT_ADDRESS(D_800DE048_de, 0xC4U), RW_MENU_RESIDENT_ADDRESS(D_800D3A9C, 0x40U), 0x00008000U, 1},
        {RW_MENU_RESIDENT_ADDRESS(D_800DE048_de, 0xACU), RW_MENU_RESIDENT_ADDRESS(D_800E1BB8, 0xD4U), 0xFFFFFFFFU, 1},
        {RW_MENU_RESIDENT_ADDRESS(D_800DE048_de, 0x98U), RW_MENU_RESIDENT_ADDRESS(D_800E1BB8, 0xF0U), 0x0400C7E0U, 1}
    },
    {
        {1, 0, 0x50000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x08000021U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0x20U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00210041U, 0, 20, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E1BB8, 0xBCU), {&rw_menu_paged_func_8043C4F4_us_rev1_offset_698}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_81C}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8F4}, 0, 0},
        {0, 0, 0x00010050U, 80, 20, {6, 1, 255, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x8U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 254, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x10U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 253, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x18U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 252, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x20U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 251, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x28U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 250, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x30U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 250, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x38U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00010050U, 80, 8, {6, 1, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x40U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x48U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x50U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x58U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x60U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x68U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 250, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x70U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00010050U, 80, 8, {6, 1, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x78U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x80U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x88U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x90U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x98U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xA0U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 250, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xA8U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00010050U, 80, 8, {6, 1, 249, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xB0U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 249, 7}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xB8U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 249, 6}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xC0U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 249, 5}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xC8U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 249, 4}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xD0U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_8FC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 1, 249, 3}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x130U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_9BC}, 0, 0},
        {0, 0, 0x00024050U, 8, 0, {255, 250, 249, 2}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x138U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_AE4}, 0, 0},
        {0, 0, 0x00011001U, 0, 20, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_C30}, {0}, 0, 0},
        {0, 0, 0x00310201U, 0, 24, {0, 0, 254, 226}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0x30U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80442384_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0xB98U), 33, 0, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_684}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 2, 0, 0, 0, 0}
    },
    {
        {1, 0, 0x50000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x08000021U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0x14U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00411001U, 0, 12, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3A9C, 0x74U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_F64}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_F48}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3A9C, 0x7CU), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_FEC}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_FD0}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3B20, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043D388_de}, {&rw_menu_paged_func_8043D36C_de}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3B28, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043D410_de}, {&rw_menu_paged_func_8043D3F4_de}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3B30, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043D498_de}, {&rw_menu_paged_func_8043D47C_de}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3B38, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043D520_de}, {&rw_menu_paged_func_8043D504_de}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3B40, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043D5A8_de}, {&rw_menu_paged_func_8043D58C_de}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3B50, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043D6B8_de}, {&rw_menu_paged_func_8043D69C_de}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3B48, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043D630_de}, {&rw_menu_paged_func_8043D614_de}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3B58, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043D740_de}, {&rw_menu_paged_func_8043D724_de}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3B60, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043D7C8_de}, {&rw_menu_paged_func_8043D7AC_de}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3B68, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043D850_de}, {&rw_menu_paged_func_8043D834_de}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3B70, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043D8D8_de}, {&rw_menu_paged_func_8043D8BC_de}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3B78, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043D960_de}, {&rw_menu_paged_func_8043D944_de}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3B80, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043D9E8_de}, {&rw_menu_paged_func_8043D9CC_de}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3B88, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043DA70_de}, {&rw_menu_paged_func_8043DA54_de}, 0, 0},
        {0, 0, 0x00411001U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_DDC}, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_374}, 0, 0},
        {0, 0, 0x00000201U, 0, 192, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0x30U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80442384_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x1060U), 20, 0, {&rw_menu_paged_func_8043C4F4_us_rev1_offset_23C}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 19, 0, 0, 0, 0}
    },
    0,
    {
        {0, 0, 0x00000021U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0x34U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00010081U, 0, 40, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0xCU), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043DADC_de_offset_4}, 0, 0},
        {0, 0, 0x00010081U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0x10U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043DB04_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x1358U), 3, 0, {0}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 1, 0, 0, 0, 0}
    },
    {
        {0, 0, 0x00011001U, 0, 0, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D3660, 0xCU), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00011001U, 0, 0, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D3660, 0x10U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00011001U, 0, 0, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D3660, 0x14U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00011001U, 0, 0, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D3660, 0x18U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00011001U, 0, 0, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D3660, 0x1CU), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00011001U, 0, 0, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D3660, 0x20U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00011001U, 0, 0, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D3660, 0x24U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 5, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D3660, 0x8U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043E1F8_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x13E8U), 8, 0, {0}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_8043E1C0_de}, 7, 0, 0, 0, ((u32)&D_0044E468 + 0x15E0U)}
    },
    {
        {0, 0, 0x00011001U, 0, 0, {0, 0, 255, 2}, RW_MENU_RESIDENT_ADDRESS(D_800D3628, 0x14U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043E418_de}, 0, 0},
        {0, 0, 0x00010101U, 0, 8, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D3628, 0x18U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043E444_de}, {0}, 0, 0},
        {0, 0, 0x00411001U, 0, 8, {0, 0, 254, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043E2D8_de}, {&rw_menu_paged_func_8043E28C_de}, 0, 0},
        {0, 0, 0x00411001U, 0, 2, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D365C, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043E494_de}, {&rw_menu_paged_func_8043E3CC_de}, 0, 0},
        {0, 0, 0x00010041U, 0, 5, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3660, 0x8U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043E468_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x152CU), 5, 0, {0}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_8043E254_de}, 4, 0, 0, 0, ((u32)&D_0044E468 + 0x16A0U)}
    },
    1120,
    11,
    {0.0f, 0.0f, -50.0f},
    -900.0f,
    {2.0f, 2.0f, 2.0f},
    50,
    80,
    0.087266475f,
    {
        {1, 0, 0x30000000U, 0, 0, {0, 0, 255, 1}, 0x00000387U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {3, 0, 0x00410021U, 0, 0, {0, 0, 255, 1}, ((u32)&D_0044E468 + 0x1604U), {&rw_menu_paged_func_80440838_de}, {&rw_menu_paged_func_8043E5D0_de}, {&rw_menu_paged_func_8043F37C_eu}, 0, 0},
        {0, 0, 0x00010081U, 0, 25, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3628, 0x10U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043E5C8_de}, {&rw_menu_paged_func_8043E568_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x1634U), 3, 0, {&rw_menu_paged_func_8043E4E0_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_8043E530_de}, 1, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x1634U), 3, 0, {&rw_menu_paged_func_8043E504_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_8043E530_de}, 1, 0, 0, 0, 0}
    },
    {
        {1, 0, 0x30000000U, 0, 0, {0, 0, 255, 1}, 0x00000387U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00010081U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3660, 0x2CU), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00010101U, 0, 12, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3660, 0x30U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043E610_de}, 0, 0},
        {0, 0, 0x00010101U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3660, 0x34U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043E658_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x16E8U), 4, 0, {0}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_8043E5D8_de}, 2, 0, 0, 0, 0}
    },
    {-0.5f, -0.400000006f, -0.300000012f, -0.200000003f, -0.100000001f, 0.0f, 0.100000001f, 0.200000003f, 0.300000012f, 0.400000006f, 0.5f, 0.600000024f, 0.699999988f, 0.800000012f, 0.899999976f, 1.0f},
    {
        {1, 0, 0x10000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x08000201U, 0, 12, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3C50, 0xC0U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00410041U, 0, 30, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043DD2C_us_rev1_offset_4}, {&rw_menu_paged_func_8043E830_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043DD2C_us_rev1_offset_4}, {&rw_menu_paged_func_8043E830_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043DD2C_us_rev1_offset_4}, {&rw_menu_paged_func_8043E830_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043DD2C_us_rev1_offset_4}, {&rw_menu_paged_func_8043E830_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043DD2C_us_rev1_offset_4}, {&rw_menu_paged_func_8043E830_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043DD2C_us_rev1_offset_4}, {&rw_menu_paged_func_8043E830_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043DD2C_us_rev1_offset_4}, {&rw_menu_paged_func_8043E830_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043DD2C_us_rev1_offset_4}, {&rw_menu_paged_func_8043E830_de}, 0, 0},
        {0, 0, 0x00010201U, 0, 45, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0x30U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80442384_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x17DCU), 11, 0, {0}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 10, 0, 0, 0, ((u32)&D_0044E468 + 0x1A1CU)}
    },
    {
        {1, 0, 0x10000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x08000021U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3C50, 0x14U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00010081U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3C50, 0xC0U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043E91C_de}, 0, 0},
        {0, 0, 0x00010201U, 0, 56, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0x30U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80442384_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x198CU), 4, 0, {0}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 2, 0, 0, 0, ((u32)&D_0044E468 + 0x1C80U)}
    },
    {
        {1, 0, 0x10000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x08000021U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3C50, 0x4U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00010081U, 0, 54, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3C50, 0x8U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043E9A8_de}, 0, 0},
        {0, 0, 0x00010081U, 0, 4, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3C50, 0xCU), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043EA00_de}, 0, 0},
        {0, 0, 0x00010201U, 0, 73, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3628, 0x8U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043EA4C_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x1A40U), 5, 0, {&rw_menu_paged_func_8043E948_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 2, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x1A40U), 5, 0, {&rw_menu_paged_func_8043E97C_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 2, 0, 0, 0, 0}
    },
    {
        {1, 0, 0x10000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x1B3CU), 1, 0, {0}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 2, 0, 0, 0, ((u32)&D_0044E468 + 0x1C80U)}
    },
    {
        {1, 0, 0x10000000U, 0, 0, {0, 0, 255, 1}, 0x00000384U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00000081U, 0, 30, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3C50, 0x4U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043EA98_de}, 0, 0},
        {0, 0, 0x00010081U, 0, 12, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3C50, 0x10U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043EB70_de}, {&rw_menu_paged_func_8043EBE4_de}, 0, 0},
        {0, 0, 0x00010081U, 0, 12, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0x20U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043EC6C_de}, 0, 0},
        {0, 0, 0x00010081U, 0, 12, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3C50, 0x12CU), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043EC98_de}, 0, 0},
        {0, 0, 0x00010081U, 0, 12, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3C50, 0x128U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043EC40_de}, 0, 0},
        {0, 0, 0x00010081U, 0, 12, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3C50, 0x138U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043ECC4_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x1B84U), 7, 0, {0}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 1, 0, 0, 0, 0}
    },
    0,
    {
        {1, 0, 0x00000000U, 0, 0, {0, 0, 0, 0}, 0x00000064U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00000089U, 0, 40, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x140U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8043EE24_de}, {0}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x1CA8U), 2, 0, {&rw_menu_paged_func_8043EF1C_us_rev1_offset_4}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_8043EDDC_de}, 1, 0, 0, 0, 0}
    },
    {2, 0},
    {
        {1, 0, 0x50000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x08000801U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D36A0, 0x2CU), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00010080U, 35, 22, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0xCU), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0}
    },
    {1, 0, 0x00024010U, 3, 0},
};
typedef char resident_menu_pak_options_extent[(sizeof(struct resident_menu_pak_options)==7860)?1:-1];
