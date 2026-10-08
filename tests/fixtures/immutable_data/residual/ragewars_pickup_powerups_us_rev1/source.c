#include "resident_pickup_descriptors.h"

extern s32 func_802AC66C_de(void *, void *);

/* 4 descriptors traversed by 802AB400/802AB6EC at stride 24.
 * Callbacks retain the cartridge encoding with the KSEG0 bias removed.
 * ROM D3DD0..D3E30. */
ResidentPowerupDescriptor D_800CDEF0[4] = {
    {{&D_800D7070[49], 2123, 3596, -1, 0}, 18, 3, 65536, (ResidentPickupHandler)((char *)func_802AC66C_de - 0x80000000U)},
    {{&D_800D7070[50], 2124, 3596, -1, 0}, 19, 4, 196608, (ResidentPickupHandler)((char *)func_802AC66C_de - 0x80000000U)},
    {{&D_800D7070[51], 2125, 3596, -1, 0}, 20, 6, 65536, (ResidentPickupHandler)((char *)func_802AC66C_de - 0x80000000U)},
    {{&D_800D7070[53], 2127, 3596, -1, 0}, 21, 5, 65536, (ResidentPickupHandler)((char *)func_802AC66C_de - 0x80000000U)},
};
