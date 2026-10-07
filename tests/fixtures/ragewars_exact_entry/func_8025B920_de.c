#include "shared/func_8025B920_de_closed.h"

s32 func_8025B920_de(RecordD90 *record, s16 value, s16 id) {
    s32 i;
    SlotCC *cur = record->slots;

    for (i = 0; i < 16; cur++, i++) {
        if (cur->used != -1 && record->header->local != i && cur->value == value && (cur->mode & 0x40) &&
            cur->id == id) {
            release(&record->slots[(s16)i]);
            return (s16)i;
        }
    }
    return -1;
}
