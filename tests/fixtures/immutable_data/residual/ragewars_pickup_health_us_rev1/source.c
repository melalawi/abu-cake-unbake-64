#include "resident_pickup_descriptors.h"

extern s32 func_802ACC04_de(void *, void *);

/* 8 descriptors traversed by 802AB400/802AB6EC at stride 24.
 * Callbacks retain the cartridge encoding with the KSEG0 bias removed.
 * ROM D3EA8..D3F68. */
ResidentHealthDescriptor D_800CDFC8_de[8] = {
    {{&D_800D7070[0], 1701, 1, 0, 0}, 2, 200, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
    {{&D_800D7070[1], 1702, 1, 0, 0}, 10, 100, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
    {{&D_800D7070[5], 1703, 1, 299, 0}, 100, 100, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
    {{&D_800D7070[6], 1704, 1, 299, 0}, 100, 200, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
    {{&D_800D7070[1], 1720, 1, 0, 0}, 10, 100, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
    {{&D_800D7070[2], 1721, 3560, 0, 0}, 15, 100, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
    {{&D_800D7070[3], 1722, 3560, 0, 0}, 25, 100, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
    {{&D_800D7070[4], 1723, 3570, 0, 0}, 50, 100, (ResidentPickupHandler)((char *)func_802ACC04_de - 0x80000000U)},
};
