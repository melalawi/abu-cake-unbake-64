#include "abi.h"
#include "span_1000/code_802B369C.h"
#include "audio_callbacks.h"
#include "span_1000/code_802B4730.h"
#include "abi.h"
#include "types.h"
#include "acmd.h"
#include "abi.h"
#include "common/unused.h"

/* alMainBusPull (libultra audio main bus): clears the main left and right buffers, then pulls each source and mixes the aux outputs into the main buffers. */
Acmd *func_802B56E0_eu_x(void *filter, s16 *outp, s32 outCount, s32 sampleOffset, Acmd *p) {
    Acmd *ptr = p;
    ALMainBus_s *m = (ALMainBus_s *)filter;
    ALFilter_s14 **sources = m->sources;
    s32 i;

    aClearBuffer(ptr++, 0x440, outCount << 1);
    aClearBuffer(ptr++, 0x580, outCount << 1);

    for (i = 0; i < m->sourceCount; i++) {
        ptr = ((ALCmdHandler)sources[i]->handler)(sources[i], outp, outCount, sampleOffset, ptr);
        aSetBuffer(ptr++, 0, 0, 0, outCount << 1);
        aMix(ptr++, 0, 0x7FFF, 0x6C0, 0x440);
        aMix(ptr++, 0, 0x7FFF, 0x800, 0x580);
    }
    return ptr;
}
