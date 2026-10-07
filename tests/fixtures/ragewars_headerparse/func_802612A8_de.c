#include "types.h"

s32 func_802612A8_de(void *arg0, void *arg1) {
    if (
#if defined(VERSION_EU)
func_8025F094_eu
#elif defined(VERSION_EU_X)
func_8025F0C4_eu_x
#elif defined(VERSION_US)
func_8025F074_us
#elif defined(VERSION_US_REV1)
func_8025F0F4_us_rev1
#else
func_8025F0D4_de
#endif
(a, c, first->n14, 3, first->x, first->z) == 0) {
        return 0;
    }
}
