#include "shared/func_80212D80_eu_x_closed.h"

void func_80212D80_eu_x(struct Actor_func_80212D78_eu_x *arg0) {
    Brain_func_80212D78_eu_x *brain = arg0->player->brain;

    brain->unk220 = 0;
    func_80209988_de(brain);
    brain->unk2FC = 0;
}
