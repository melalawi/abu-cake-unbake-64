#include "shared/func_8025B920_de_closed.h"

s32 func_8025B920_de(void *record, s16 value, s16 id) {
    s32 i;
    SlotCC *cur = ((RecordD90 *)record)->slots;

    for (i = 0; i < 16; cur++, i++) {
        if (cur->used != -1 && ((RecordD90 *)record)->header->local != i && cur->value == value && (cur->mode & 0x40) &&
            cur->id == id) {
            release(&((RecordD90 *)record)->slots[(s16)i]);
            return (s16)i;
        }
    }
    return -1;
}
