#include "span_C76B0/data.h"
#include "types.h"
#include "common/unused.h"

extern f32 D_800CD738;

extern s32 func_80265878_de(StateCallback);
extern s32 func_80265784_de(u32);
extern void *func_8024B738_de(void *, void *, s32);
extern s32 func_80246A08_de(void *, s32, s32);
extern s32 func_8024B6F4_de(void *, s32, s32);

void func_80213ED4_de(Source110 *src, Dest *dst) {
    StateEntry *entry;
    StateCallback callback;
    f32 factor;

    entry = dst->entry;
    if (entry != 0) {
        if (dst->old_state != dst->state) {
            if (entry->scale >= 0.0f) {
                dst->entry_scale = entry->scale;
            }
            dst->old_entry_flags = dst->entry_flags;
            dst->entry_flags = entry->flags;
            src->state_changed = 0;
            callback = entry->callback;
            if (callback != 0) {
                if (func_80265878_de(callback) != 0) {
                    callback = callback(src, dst);
                } else if (func_80265784_de((u32)callback) != 0) {
                    callback = (StateCallback)func_8024B738_de(src, callback, dst->state_result);
                }
                if (callback != 0) {
                    if (callback != (StateCallback)-1) {
                        dst->state_result = func_80246A08_de(src, (s32)callback, dst->state_result);
                        dst->result = (s16)callback;
                        if (src->current_state == dst->state_result) {
                            src->limit = 0.0f;
                        }
                    }
                    dst->count = 0;
                    dst->flags &= ~1U;
                }
            }
            dst->old_state = (u8)dst->state;
        } else if ((dst->state_result == -1) || (dst->result == -1)) {
            dst->count++;
            dst->flags |= 1;
        } else if (src->current_state == dst->state_result) {
            if (!(dst->flags & 1)) {
                s32 difference = src->counter - entry->threshold;
                double converted = difference;
                if (difference < 0) {
                    converted += D_800C2130_de;
                }
                if ((f32)converted <= src->limit) {
                    dst->count++;
                    dst->flags |= 1;
                }
            }
            if (src->state_changed != 0) {
                src->state_changed = 0;
                if (!(dst->flags & 1)) {
                    dst->count++;
                }
                dst->flags &= ~1U;
            }
        }

        if (dst->entry_flags & 0x10) {
            factor = dst->entry_scale;
        } else {
            factor = dst->scale * dst->entry_scale;
        }
        D_800CD738 *= factor;
        func_8024B6F4_de(src, dst->state_result, (dst->entry_flags >> 5) & 1);
        if ((dst->state_result != -1) && (src->previous_state != dst->state_result)) {
            dst->count = 0;
            dst->flags &= ~1U;
            src->state_changed = 0;
        }
        if (src->flags & 0x08000000) {
            dst->count++;
        }
    }
}

/* Native resident constant storage; absolute access symbols retain their addresses. */
#if defined(VERSION_US)
const double unbake_rodata_800C2060_8 = 4294967296.0;
#elif defined(VERSION_US_REV1)
const double unbake_rodata_800C7220_8 = 4294967296.0;
#elif defined(VERSION_EU)
const double unbake_rodata_800C23D0_8 = 4294967296.0;
#elif defined(VERSION_EU_X)
const double unbake_rodata_800C2410_8 = 4294967296.0;
#elif defined(VERSION_DE)
const double unbake_rodata_800C2130_8 = 4294967296.0;
#endif
