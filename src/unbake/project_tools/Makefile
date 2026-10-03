# @TITLE@ matching decompilation.
# make setup; make extract; make; make clean; make distclean
# VERSION: @VERSIONS@. COMPARE=0 skips comparison.
.DEFAULT_GOAL := all
.SUFFIXES:
.DELETE_ON_ERROR:
.SECONDARY:
VERSIONS := @VERSIONS@
COMPARE ?= 1
NON_MATCHING ?= 0
ifeq ($(filter $(NON_MATCHING),0 1),)
$(error HELD(build): NON_MATCHING must be 0 or 1)
endif
ifeq ($(NON_MATCHING),1)
override COMPARE := 0
BUILD ?= @BUILD_ROOT@/$(VERSION).nonmatching
else
BUILD ?= @BUILD_ROOT@/$(VERSION)
endif
ROM := $(BUILD)/@NAME@.$(VERSION).z64
ELF := $(BUILD)/@NAME@.elf
LD_SCRIPT := $(BUILD)/@NAME@.ld
TOOLS := @TOOLS@
SRC := @SRC@
ASM := @ASM@/$(VERSION)
resolve-tool = $(or $(shell python3 $(TOOLS)/host.py $(1)),$(error HELD(build): missing tool $(1)))
LD = $(call resolve-tool,@LD@)
OBJCOPY = $(call resolve-tool,@OBJCOPY@)
SPLAT = $(call resolve-tool,@SPLAT@)
PINS := @PINS@
RECIPE := $(TOOLS)/build.json
LINK_RECIPE := $(TOOLS)/link.json
EXTRACT_RECIPE := $(TOOLS)/extract.json

ifeq ($(strip $(VERSION)),)
ifneq ($(origin BUILD),file)
$(error HELD(build): BUILD requires VERSION)
endif
GOALS := $(filter-out distclean,$(if $(MAKECMDGOALS),$(MAKECMDGOALS),all))
$(foreach goal,$(GOALS),$(eval $(goal): $(addprefix $(goal)-,$(VERSIONS))))
define dispatch
$(1)-$(2):
	+$$(MAKE) VERSION=$(2) $(1)
.PHONY: $(1)-$(2)
endef
$(foreach goal,$(GOALS),$(foreach version,$(VERSIONS),$(eval $(call dispatch,$(goal),$(version)))))

distclean:
	rm -rf -- @BUILD_ROOT@ @ASM@

.PHONY: $(GOALS) distclean
else
ifeq ($(filter $(VERSION),$(VERSIONS)),)
$(error HELD(build): unknown VERSION=$(VERSION))
endif
@VERSION_BLOCKS@

all: $(ROM)
ifeq ($(COMPARE),1)
	@sed 's|  .*|  $(ROM)|' versions/$(VERSION)/@NAME@.sha1 | sha1sum -c -
endif

verify:
	@sha256sum -c $(PINS) > /dev/null

setup: verify
	sha1sum -c versions/$(VERSION)/baserom.sha1

extract: $(BUILD)/.split

ifeq ($(BUILD),@BUILD_ROOT@/$(VERSION))
prepare-build:
	python3 $(TOOLS)/extract.py prepare-build --build $(BUILD)

$(BUILD)/.split.mk: | prepare-build
.PHONY: prepare-build
endif

ifneq ($(filter-out setup clean distclean,$(if $(MAKECMDGOALS),$(MAKECMDGOALS),all)),)
include $(BUILD)/.split.mk
endif

# Partial builds select guarded C rows during extraction. Matching builds use
# the split alone for ownership; editing C only invalidates its object receipt.
ifeq ($(NON_MATCHING),1)
$(BUILD)/.split.mk: $(wildcard $(SRC)/*.c)
endif
$(BUILD)/.split.mk: $(BASEROM) $(SPLIT) $(SYMBOLS) $(TOOLS)/extract.py $(TOOLS)/rodata.py $(EXTRACT_RECIPE)
	@mkdir -p $(BUILD)
	python3 $(TOOLS)/extract.py --split $(SPLIT) --symbols $(SYMBOLS) --baserom $(BASEROM) --build $(BUILD) --asm $(ASM) --src $(SRC) --name @NAME@ --splat $(SPLAT) --recipe $(EXTRACT_RECIPE) --non-matching $(NON_MATCHING)

$(BUILD)/.split: $(BUILD)/.split.mk
	python3 $(TOOLS)/atomic.py --touch $@

$(LD_SCRIPT) $(LINK_SCRIPTS) $(BUILD)/symbol-addresses.txt $(BUILD)/unit-ranges.json: | $(BUILD)/.split
	@test -f $@ || { printf '%s\n' 'HELD(extract): missing $@; remove $(BUILD)/.split.mk and make extract'; exit 1; }

@COMPILE_RULES@

# Group only missing receipts. Existing objects retain their individual rules,
# including header dependency tracking and unchanged-object timestamp behavior.
C_COLD := $(filter-out $(wildcard $(C_OBJECTS:.o=.built)),$(C_OBJECTS:.o=.built))
define compile-chunk
$(1) &: $(patsubst $(BUILD)/obj/src/%.built,$(SRC)/%.c,$(1)) | verify
	python3 $(TOOLS)/compile.py --kind cc --non-matching $(NON_MATCHING) --recipe $(RECIPE) --version $(VERSION) --unit batch --source $(SRC) --output $(BUILD)/obj/src --batch $(patsubst $(BUILD)/obj/src/%.built,$(SRC)/%.c,$(1))
endef
define compile-chunks
$(if $(strip $(1)),$(eval $(call compile-chunk,$(wordlist 1,128,$(1))))$(call compile-chunks,$(wordlist 129,$(words $(1)),$(1))))
endef
$(call compile-chunks,$(C_COLD))

ASM_COLD := $(filter-out $(wildcard $(ASM_OBJECTS:.o=.built)),$(ASM_OBJECTS:.o=.built))
define assemble-chunk
$(1) &: $(patsubst $(BUILD)/obj/asm/%.built,$(ASM)/%.s,$(1)) | verify
	python3 $(TOOLS)/compile.py --kind as --non-matching $(NON_MATCHING) --recipe $(RECIPE) --version $(VERSION) --unit batch --source $(ASM) --output $(BUILD)/obj/asm --symbols $(BUILD)/asm-symbols --batch $(patsubst $(BUILD)/obj/asm/%.built,$(ASM)/%.s,$(1))
endef
define assemble-chunks
$(if $(strip $(1)),$(eval $(call assemble-chunk,$(wordlist 1,128,$(1))))$(call assemble-chunks,$(wordlist 129,$(words $(1)),$(1))))
endef
$(call assemble-chunks,$(ASM_COLD))

$(BUILD)/obj/src/%.built: $(SRC)/%.c | verify
	@mkdir -p $(@D)
	python3 $(TOOLS)/compile.py --kind cc --non-matching $(NON_MATCHING) --recipe $(RECIPE) --version $(VERSION) --unit $(SRC)/$*.c --source $< --output $(@:.built=.o) --depfile $(@:.built=.d) --dep-target='$$(BUILD)/obj/src/$*.built'
	python3 $(TOOLS)/atomic.py --touch $@

$(BUILD)/obj/src/%.o: $(BUILD)/obj/src/%.built
	@test -f $@

$(BUILD)/obj/asm/%.built: $(ASM)/%.s | verify
	@mkdir -p $(@D)
	python3 $(TOOLS)/compile.py --kind as --non-matching $(NON_MATCHING) --recipe $(RECIPE) --version $(VERSION) --unit $* --source $< --output $(@:.built=.o) --symbols $(BUILD)/asm-symbols/$*.txt --depfile $(@:.built=.d) --dep-target='$$(BUILD)/obj/asm/$*.built'
	python3 $(TOOLS)/atomic.py --touch $@

$(BUILD)/obj/asm/%.o: $(BUILD)/obj/asm/%.built
	@test -f $@

$(BUILD)/obj/assets/%.bin.o: $(ASM)/assets/%.bin $(OBJCOPY) $(TOOLS)/atomic.py
	@mkdir -p $(@D)
	python3 $(TOOLS)/atomic.py --output $@ -- $(OBJCOPY) -I binary -O elf32-tradbigmips -B mips $< $@

$(ELF): $(SYMBOLS) $(OBJECTS) $(LD_SCRIPT) $(LINK_SCRIPTS) $(TOOLS)/layout.py $(TOOLS)/rodata.py $(TOOLS)/literal_layout.py $(TOOLS)/pool_slices.py $(TOOLS)/elf.py $(TOOLS)/extract.py $(TOOLS)/atomic.py $(LINK_RECIPE) $(BASEROM) $(BUILD)/unit-ranges.json $(LD)
	@mkdir -p $(@D)
	python3 $(TOOLS)/layout.py --script $(LD_SCRIPT) --output $(BUILD)/@NAME@.link.ld --build $(BUILD) --ranges $(BUILD)/unit-ranges.json --recipe $(LINK_RECIPE) --version $(VERSION) --baserom $(BASEROM) --non-matching $(NON_MATCHING)
	cd $(BUILD) && LC_ALL=C python3 $(abspath $(TOOLS))/atomic.py --output @NAME@.elf --output @NAME@.map -- $(LD) $$(cat @NAME@.link.flags) -T @NAME@.link.ld $(addprefix -T ,$(abspath $(LINK_SCRIPTS))) -Map @NAME@.map -o @NAME@.elf $(patsubst $(BUILD)/%,%,$(OBJECTS))

$(ROM): $(ELF) $(OBJCOPY) $(TOOLS)/atomic.py
	python3 $(TOOLS)/atomic.py --output $@ -- $(OBJCOPY) -O binary --pad-to $(ROM_BYTES) $< $@

clean:
	@test -n '$(BUILD)' && test '$(BUILD)' != / && test '$(BUILD)' != .
	rm -rf -- '$(BUILD)'

distclean: clean
	rm -rf -- @ASM@/$(VERSION)

ifneq ($(filter-out setup clean distclean,$(if $(MAKECMDGOALS),$(MAKECMDGOALS),all)),)
-include $(DEPFILES)
endif
check: $(ROM)
ifeq ($(NON_MATCHING),1)
	@printf '%s\n' 'HELD(check): NON_MATCHING=1 cannot verify a matching cartridge'; exit 1
else
	@sed 's|  .*|  $(ROM)|' versions/$(VERSION)/@NAME@.sha1 | sha1sum -c -
endif

.PHONY: all check verify setup extract clean distclean

endif
