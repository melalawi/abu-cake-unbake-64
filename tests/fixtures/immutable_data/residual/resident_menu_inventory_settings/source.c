#include "menu_rom_records.h"
#include "menu_rom_code_addresses.h"
#include "common/unused.h"
#include "span_C76B0/data.h"

/* Existing paged list address label; declaration preserved from its consumers. */
extern char D_0044E468[];

/* US-rev1 ROM1BEE48..1C0860, compiled at resident80156000.
 * Stored addresses retain the paged004xxxxx representation; no callable
 * C ABI or backing object is invented for a code-address label.
 * Ownership splits the descriptor at virtual00450E3C into prefix/tail.
 * The final debug-name prefix continues outside the second claim. */
struct resident_menu_inventory_settings {
    MenuRomDescriptorTail inventory_entry_3_tail;
    MenuRomDescriptor entries_450E60[45];
    MenuRomList lists_4514B4[1];
    u8 settings_navigation[4];
    s32 settings_mode;
    MenuRomDescriptor entries_4514E0[1];
    MenuRomList lists_451504[2];
    s8 settings_navigation_deltas[4];
    MenuRomDescriptor entries_451550[3];
    MenuRomDescriptor entries_4515BC[3];
    MenuRomDescriptor entries_451628[3];
    MenuRomDescriptor entries_451694[3];
    MenuRomList lists_451700[12];
    MenuRomDescriptor entries_4518B0[16];
    MenuRomList lists_451AF0[1];
    s32 settings_counter;
    u16 settings_coordinates[2];
    u32 settings_field_451B1C;
    MenuRomDescriptor entries_451B20[5];
    MenuRomList lists_451BD4[1];
    MenuRomDescriptor entries_451BF8[8];
    MenuRomList lists_451D18[1];
    MenuRomDescriptor entries_451D3C[7];
    MenuRomList lists_451E38[1];
    MenuRomDescriptor entries_451E5C[7];
    MenuRomList lists_451F58[1];
    MenuRomDescriptor entries_451F7C[16];
    MenuRomList lists_4521BC[1];
    MenuRomDescriptor entries_4521E0[45];
    MenuRomList lists_452834[1];
    u32 debug_value_prefix;
    char debug_name_prefix[4];
};
const struct resident_menu_inventory_settings resident_menu_inventory_settings = {
    {{0, 0, 0, 0}, 0x000007D0U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4310, 0},
    {
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D0U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4310, 1},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D0U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4310, 2},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D1U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4320, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D1U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4320, 1},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D1U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4320, 2},
        {1, 0, 0x00024010U, 41, 0, {0, 0, 0, 0}, 0x000007E4U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4360, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x00000820U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4500, 0},
        {0, 0, 0x00010080U, 35, 3, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x10U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D2U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4330, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D2U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4330, 1},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D2U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4330, 2},
        {1, 0, 0x00024010U, 60, 0, {0, 0, 0, 0}, 0x0000080DU, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4402, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007F9U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4382, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007E5U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4361, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x00000820U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4500, 1},
        {0, 0, 0x00010080U, 35, 3, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x14U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D3U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4340, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D3U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4340, 1},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D3U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4340, 2},
        {1, 0, 0x00024010U, 60, 0, {0, 0, 0, 0}, 0x0000080FU, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4400, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007F8U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4380, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007E6U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4362, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x00000820U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4500, 2},
        {0, 0, 0x00010080U, 35, 3, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x18U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D4U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4350, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D4U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4350, 1},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D4U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4350, 2},
        {1, 0, 0x00024010U, 60, 0, {0, 0, 0, 0}, 0x0000080EU, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4404, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007FCU, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4384, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007E7U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4363, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x00000820U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4500, 3},
        {0, 0, 0x00010080U, 35, 3, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x1CU), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D4U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4350, 3},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D4U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4350, 4},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007D4U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4350, 5},
        {1, 0, 0x00024010U, 60, 0, {0, 0, 0, 0}, 0x0000080CU, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4403, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007FBU, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4383, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007E8U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4364, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x00000820U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4500, 4},
        {0, 0, 0x00010080U, 35, 3, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x20U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {1, 0, 0x00024010U, 117, 0, {0, 0, 0, 0}, 0x00000810U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4401, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007FAU, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4381, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x000007E9U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4365, 0},
        {1, 0, 0x00024010U, 3, 0, {0, 0, 0, 0}, 0x00000820U, {&rw_menu_paged_func_8043EE5C_de}, {0}, {0}, 4500, 5},
        {0, 0, 0x00010081U, 0, 30, {0, 0, 0, 0}, RW_MENU_RESIDENT_ADDRESS(D_800D36A0, 0x30U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_8043F0C8_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x1D18U), 49, 0, {&rw_menu_paged_func_8043F040_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 48, 0, 0, 0, ((u32)&D_0044E468 + 0x3104U)}
    },
    {0, 1, 1, 16},
    4,
    {
        {1, 0, 0x50000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x2428U), 1, 0, {&rw_menu_paged_func_80444314_eu}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 9, 0, 0, 0, ((u32)&D_0044E468 + 0x2A38U)},
        {((u32)&D_0044E468 + 0x2428U), 1, 0, {&rw_menu_paged_func_804433F8_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 9, 0, 0, 0, 0}
    },
    {-1, 1, -8, 8},
    {
        {1, 0, 0x50000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00000401U, 0, 40, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3BBC, 0x8U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_804434E0_de}, {0}, 0, 0},
        {1, 0, 0x20010001U, 0, 0, {0, 0, 0, 1}, 0x0000012DU, {&rw_menu_paged_func_80443734_de}, {0}, {&rw_menu_paged_func_80443710_de}, 0, 0}
    },
    {
        {1, 0, 0x50000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00000401U, 0, 40, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3BBC, 0x28U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_804434E0_de}, {0}, 0, 0},
        {1, 0, 0x20010001U, 65516, 0, {236, 0, 0, 1}, 0x0000012DU, {&rw_menu_paged_func_80443734_de}, {0}, {&rw_menu_paged_func_80443710_de}, 0, 0}
    },
    {
        {1, 0, 0x50000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00000401U, 0, 40, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3BBC, 0x48U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_804434E0_de}, {0}, 0, 0},
        {1, 0, 0x20010001U, 0, 0, {0, 0, 0, 1}, 0x0000012DU, {&rw_menu_paged_func_80443734_de}, {0}, {&rw_menu_paged_func_80443710_de}, 0, 0}
    },
    {
        {1, 0, 0x50000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00000401U, 0, 40, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3BBC, 0x68U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_804434E0_de}, {0}, 0, 0},
        {1, 0, 0x20010001U, 0, 0, {0, 0, 0, 1}, 0x0000012DU, {&rw_menu_paged_func_80443734_de}, {0}, {&rw_menu_paged_func_80443710_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x2498U), 3, 0, {&rw_menu_paged_func_80443C14_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_804438BC_de}, 2, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x2504U), 3, 0, {&rw_menu_paged_func_80443C14_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_804438BC_de}, 2, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x2570U), 3, 0, {&rw_menu_paged_func_80443C14_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_804438BC_de}, 2, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x25DCU), 3, 0, {&rw_menu_paged_func_80443C14_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_804438BC_de}, 2, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x2498U), 3, 0, {&rw_menu_paged_func_80443CD4_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 14, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x2504U), 3, 0, {&rw_menu_paged_func_80443CD4_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 14, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x2570U), 3, 0, {&rw_menu_paged_func_80443CD4_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 14, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x25DCU), 3, 0, {&rw_menu_paged_func_80443CD4_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 14, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x2498U), 3, 0, {&rw_menu_paged_func_80443DD0_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 14, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x2504U), 3, 0, {&rw_menu_paged_func_80443DD0_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 14, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x2570U), 3, 0, {&rw_menu_paged_func_80443DD0_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 14, 0, 0, 0, 0},
        {((u32)&D_0044E468 + 0x25DCU), 3, 0, {&rw_menu_paged_func_80443DD0_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 14, 0, 0, 0, 0}
    },
    {
        {1, 0, 0x50000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x08000021U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3C50, 0x128U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00400041U, 0, 47, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D35A8, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80444314_de}, {&rw_menu_paged_func_8044421C_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D35B4, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80444424_de}, {&rw_menu_paged_func_804443C0_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D35C0, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_804444EC_de}, {&rw_menu_paged_func_80444488_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D35C8, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_804445AC_de}, {&rw_menu_paged_func_80444548_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D35D4, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80444674_de}, {&rw_menu_paged_func_80444610_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D35D8, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8044472C_de}, {&rw_menu_paged_func_804446D8_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D35E4, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_804447F0_de}, {&rw_menu_paged_func_804447A0_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D35F4, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_8044488C_de}, {&rw_menu_paged_func_80444920_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D35FC_de, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_804449EC_de}, {&rw_menu_paged_func_80444988_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3604_de, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80444B1C_de}, {&rw_menu_paged_func_80444AB8_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D360C, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80444BE8_de}, {&rw_menu_paged_func_80444C34_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3610, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80444C68_de}, {&rw_menu_paged_func_80444CF8_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3628, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80444D9C_de}, {&rw_menu_paged_func_80444D50_de}, 0, 0},
        {0, 0, 0x00010201U, 0, 19, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3628, 0x8U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80442384_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x27F8U), 16, 0, {&rw_menu_paged_func_804441E4_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 15, 0, 0, 0, 0}
    },
    148,
    {2048, 3072},
    0,
    {
        {1, 0, 0x50000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x08000041U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D36A0, 0x28U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00000101U, 0, 93, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0x4U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00010101U, 0, 10, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0xCU), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80444E30_de}, 0, 0},
        {0, 0, 0x00010101U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0x10U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80442384_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x2A68U), 5, 0, {&rw_menu_paged_func_80444E70_de}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 4, 0, 0, 0, ((u32)&D_0044E468 + 0x3104U)}
    },
    {
        {0, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0x10U), {&rw_menu_paged_func_8044208C_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00410041U, 0, 40, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0xA8U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8044524C_us_rev1}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0xC0U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445288_us_rev1}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0xD8U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_804452C4_us_rev1}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0xF0U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445304_us_rev1}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0x108U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445344_us_rev1}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0x120U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445384_us_rev1}, 0, 0},
        {0, 0, 0x00010101U, 0, 20, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0x138U), {&rw_menu_paged_func_8044208C_de}, {&rw_menu_paged_func_804453E0_us_rev1}, {&rw_menu_paged_func_8043D25C_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x2B40U), 8, 0, {0}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 1, 0, 0, 0, ((u32)&D_0044E468 + 0x3104U)}
    },
    {
        {0, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0x20U), {&rw_menu_paged_func_8044208C_de}, {0}, {0}, 0, 0},
        {2, 0, 0x00010041U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0x14CU), {&rw_menu_paged_func_804404A8_de}, {&rw_menu_paged_func_804454B4_us_rev1}, {&rw_menu_paged_func_80442384_de}, 0, 0},
        {2, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0x160U), {&rw_menu_paged_func_804404A8_de}, {&rw_menu_paged_func_80445544_us_rev1}, {&rw_menu_paged_func_80442384_de}, 0, 0},
        {2, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0x174U), {&rw_menu_paged_func_804404A8_de}, {&rw_menu_paged_func_804455D4_us_rev1}, {&rw_menu_paged_func_80442384_de}, 0, 0},
        {2, 0, 0x00010041U, 0, 4, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0x188U), {&rw_menu_paged_func_804404A8_de}, {&rw_menu_paged_func_80445704_us_rev1}, {&rw_menu_paged_func_80442384_de}, 0, 0},
        {2, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0x19CU), {&rw_menu_paged_func_804404A8_de}, {&rw_menu_paged_func_80445794_us_rev1}, {&rw_menu_paged_func_80442384_de}, 0, 0},
        {2, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0x1B0U), {&rw_menu_paged_func_804404A8_de}, {&rw_menu_paged_func_80445824_us_rev1}, {&rw_menu_paged_func_80442384_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x2C84U), 7, 0, {0}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 1, 0, 0, 0, ((u32)&D_0044E468 + 0x3104U)}
    },
    {
        {1, 0, 0x50000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x08002001U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3C50, 0x138U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00410041U, 0, 50, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3D98, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_804451D0_de}, {&rw_menu_paged_func_80445160_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3DA8, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80445310_de}, {&rw_menu_paged_func_804452B0_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3DB8, 0x0U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80445080_de}, {&rw_menu_paged_func_80445020_de}, 0, 0},
        {0, 0, 0x00410041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3DB8, 0x4U), {&rw_menu_paged_func_80442064_de}, {&rw_menu_paged_func_80445494_de}, {&rw_menu_paged_func_804453F0_de}, 0, 0},
        {0, 0, 0x00410401U, 0, 68, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3628, 0x8U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80444F98_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x2DA4U), 7, 0, {0}, {0}, {&rw_menu_paged_func_804415F4_de}, {0}, 2, 0, 0, 0, 0}
    },
    {
        {1, 0, 0x50000000U, 0, 0, {0, 0, 255, 1}, 0x00000385U, {&rw_menu_paged_func_804402DC_de}, {0}, {0}, 0, 0},
        {0, 0, 0x08000041U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D36A0, 0x8U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00010041U, 0, 40, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D36A0, 0xCU), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80444E7C_de}, 0, 0},
        {0, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D36A0, 0x10U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80444EB4_de}, 0, 0},
        {0, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D36A0, 0x18U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80444EBC_de}, 0, 0},
        {0, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D36A0, 0x20U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80444EE8_de}, 0, 0},
        {0, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3C50, 0x138U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80445504_de}, 0, 0},
        {0, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D3A58, 0x20U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80444F14_de}, 0, 0},
        {0, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D36A0, 0x24U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80444F40_de}, 0, 0},
        {0, 0, 0x00410101U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E6090, 0x0U), {&rw_menu_paged_func_8044208C_de}, {&rw_menu_paged_func_80446000_us_rev1}, {&rw_menu_paged_func_80445FC8_us_rev1}, 0, 0},
        {0, 0, 0x00410101U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E60E4, 0x0U), {&rw_menu_paged_func_8044208C_de}, {&rw_menu_paged_func_80446090_us_rev1}, {&rw_menu_paged_func_80446048_us_rev1}, 0, 0},
        {0, 0, 0x00410101U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E6100, 0x0U), {&rw_menu_paged_func_8044208C_de}, {&rw_menu_paged_func_8044610C_us_rev1}, {&rw_menu_paged_func_804460C4_us_rev1}, 0, 0},
        {0, 0, 0x00010101U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0x10U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80446140_us_rev1}, 0, 0},
        {0, 0, 0x00010101U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E611C, 0x20U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_8044616C_us_rev1}, 0, 0},
        {0, 0, 0x00410101U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E6074, 0x0U), {&rw_menu_paged_func_8044208C_de}, {&rw_menu_paged_func_80446230_us_rev1}, {&rw_menu_paged_func_80446214_us_rev1}, 0, 0},
        {0, 0, 0x00010041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D36A0, 0x28U), {&rw_menu_paged_func_80442064_de}, {0}, {&rw_menu_paged_func_80444F6C_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x2EC4U), 16, 0, {&rw_menu_paged_func_8044560C_de}, {&rw_menu_paged_func_8044569C_de}, {&rw_menu_paged_func_804456B8_de}, {0}, 2, 0, 0, 0, 0}
    },
    {
        {0, 0, 0x00000041U, 0, 0, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800D26A4, 0x38U), {&rw_menu_paged_func_80442064_de}, {0}, {0}, 0, 0},
        {0, 0, 0x00210101U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E63BC, 0xC4U), {&rw_menu_paged_func_80445BC0_de}, {&rw_menu_paged_func_80445964_de}, {0}, 0, 0},
        {0, 0, 0x00210101U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E63BC, 0xC8U), {&rw_menu_paged_func_80445BC0_de}, {&rw_menu_paged_func_80445964_de}, {0}, 0, 0},
        {0, 0, 0x00210101U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E63BC, 0xCCU), {&rw_menu_paged_func_80445BC0_de}, {&rw_menu_paged_func_80445964_de}, {0}, 0, 0},
        {0, 0, 0x00210101U, 0, 8, {0, 0, 255, 1}, RW_MENU_RESIDENT_ADDRESS(D_800E63BC, 0xD0U), {&rw_menu_paged_func_80445BC0_de}, {&rw_menu_paged_func_80445964_de}, {0}, 0, 0},
        {0, 0, 0x00010110U, 0, 10, {7, 1, 32, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x8U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 32, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x10U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 32, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x18U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 32, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x20U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 32, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x28U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 32, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x30U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 32, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x38U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 249, 32, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x40U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00010110U, 0, 4, {7, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x48U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x50U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x58U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x60U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x68U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x70U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x78U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 249, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x80U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00010110U, 0, 4, {7, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x88U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x90U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x98U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xA0U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xA8U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xB0U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xB8U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 249, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xC0U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00010110U, 0, 4, {7, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xC8U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xD0U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xD8U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xE0U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xE8U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xF0U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0xF8U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 249, 248, 8}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x100U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00010110U, 0, 4, {7, 1, 248, 224}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x108U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 224}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x110U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 224}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x118U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 224}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x120U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 224}, RW_MENU_RESIDENT_ADDRESS(D_800D2A90, 0x4U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 224}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x128U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445AB4_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 1, 248, 224}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x130U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445D6C_de}, 0, 0},
        {0, 0, 0x00024110U, 6, 0, {255, 249, 248, 224}, RW_MENU_RESIDENT_ADDRESS(D_800D3E14_de, 0x138U), {&rw_menu_paged_func_8044208C_de}, {0}, {&rw_menu_paged_func_80445E04_de}, 0, 0}
    },
    {
        {((u32)&D_0044E468 + 0x3128U), 45, 0, {&rw_menu_paged_func_80446330_us_rev1}, {0}, {&rw_menu_paged_func_804415F4_de}, {&rw_menu_paged_func_80445EF4_de}, 5, 0, 0, 0, 0}
    },
    0,
    "\014par",
};
typedef char resident_menu_inventory_settings_extent[(sizeof(struct resident_menu_inventory_settings)==6680)?1:-1];
