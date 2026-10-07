#include "common/types_06e4f7ef1f9e.h"
#include "common/types_1dc8418c21db.h"
#include "types.h"


#include "shared/func_80212FDC_eu_layout.h"

/** Reset two state words reached through the object's linked records. */
void func_80212FDC_eu(void *object) {
    void *first = ((func_8020A028_S3 *)(object))->unk1D8;
    void *second = ((func_80212828_S2 *)(first))->unk1454;
    ((func_80212FBC_S3 *)(second))->unk220 = 0;
    ((func_80212FBC_S3 *)(second))->unk224 = -1;
}
