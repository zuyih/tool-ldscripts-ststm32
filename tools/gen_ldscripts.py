#!/usr/bin/env python3
"""Generate STM32CubeIDE-style linker scripts for whole STM32 families.

The scripts of a family only differ in the memory sizes and in the device name
of the header comment.  Everything else is the body carried by
``tools/templates/``, where those spots are ``$``-placeholders.  Nothing is
read back out of the package, so the script also runs on a family whose
directory does not exist yet.

``default.ld`` started out as the STM32CubeIDE-generated scripts that ship in
the classic STM32Cube firmware packages, with the normalisations this package
applies to all of them: no ``READONLY`` output section type, 8-digit flash
ORIGIN, ``KBytes`` spelling, a blank line after ``SECTIONS {`` and the generic
``@brief`` header instead of the board-specific ``Abstract`` one.

Data sources:

* Device list, flash size and total RAM size come from the STM32CubeIDE
  product database (``stm32targets.xml``), which lists every part number of a
  family together with its memory regions.
* The split of the total RAM into banks comes from ``RAM_BANKS`` below, which
  reproduces the MEMORY block of ST's own linker scripts for the sub-family
  (the ones in the STM32Cube firmware packages, either under
  ``Drivers/CMSIS/Device/ST/*/Source/Templates/gcc/linker`` or next to a board
  project under ``Projects/*/STM32CubeIDE``).  A sub-family that is not listed
  there gets a single RAM region at 0x20000000 whose length is the total RAM
  size, which is what ST's own scripts use for STM32H5 and STM32U0.

  Take those scripts from a current firmware package: the STM32U356/U366
  headers of the older framework-stm32cubeu3 put SRAM2 at 0x20030000, while
  both the STM32U3xx scripts and the headers of the current package put it
  right behind the 128K SRAM1 at 0x20020000.

* ``RAM_EXCLUDED`` takes back the part of that total which is not ordinary
  RAM, currently only the backup SRAM of STM32U0.

Usage:

    tools/gen_ldscripts.py stm32h5 stm32n6 stm32u0 stm32u3
    tools/gen_ldscripts.py --dry-run stm32h5
    tools/gen_ldscripts.py --targets /path/to/stm32targets.xml stm32u3
"""

import argparse
import collections
import glob
import os
import string
import sys
import xml.etree.ElementTree as ET

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(TOOLS_DIR, "templates")
REPO_DIR = os.path.dirname(TOOLS_DIR)

# STM32CubeIDE ships its product database in the "productdb" plugin.  The
# version suffix changes with every IDE release, hence the glob.
TARGETS_GLOB = os.path.join(
    "/opt/stm32cubeide/plugins",
    "com.st.stm32cube.ide.mcu.productdb_*",
    "resources/board_def/stm32targets.xml",
)

NS = "{http://st.com/stm32TargetDefinitions}"

K = 1024

# ``template`` is the body in tools/templates/ that every script of the family
# is rendered from.  ``style`` picks how a script is named and rendered:
#
# "cubeide"   <part>_FLASH.ld, one per part number, the layout STM32CubeIDE
#             generates.  Read by the `stm32cube` framework of platform-ststm32.
# "default"   <first 11 characters>_DEFAULT.ld, the name the `stm32cube`
#             framework falls back to, for a family that has no internal flash
#             to name a script after.  It substitutes nothing but ``$device``,
#             the first 9 characters of the part number; the template spells
#             out the rest, ``STM32N647`` -> ``${device}XX``.
#
# ``stack_limit`` ("cubeide" only) adds the _sstack symbol.  It is not
# cosmetic: the Armv8-M startup code of ST loads MSPLIM from it (``ldr r0,
# =_sstack`` / ``msr MSPLIM, r0`` in startup_stm32h5xx.s and
# startup_stm32u3xx.s), so a script without it fails to link with "undefined
# reference to `_sstack'".  The Cortex-M0+ STM32U0 has no MSPLIM and ST defines
# no such symbol for it.
Family = collections.namedtuple(
    "Family", "template style stack_limit", defaults=(False,)
)

FAMILIES = {
    "stm32h5": Family("default.ld", "cubeide", stack_limit=True),
    # No internal flash at all: the template is ST's LRUN script, which runs
    # the whole image out of AXISRAM, and already carries _sstack.
    "stm32n6": Family("stm32n6.ld", "default"),
    "stm32u0": Family("default.ld", "cubeide"),
    "stm32u3": Family("default.ld", "cubeide", stack_limit=True),
}

STACK_TOP = '_estack = ORIGIN(RAM) + LENGTH(RAM); /* end of "RAM" Ram type memory */'
STACK_LIMIT = (
    "_sstack = _estack - _Min_Stack_Size;"
    " /* stack limit, loaded into MSPLIM by the startup code */"
)

# Sub-family (first 9 characters of the part number) -> RAM banks, as
# (region name, attributes, origin, length in bytes, listed in header comment).
#
# All STM32U3 SRAM banks are contiguous, so a single RAM region of the total
# size would work just as well; the split is what ST's own linker scripts use,
# and the entries below reproduce them.  SRAM2 does not sit at a fixed address
# across the series: it follows SRAM1, whose size varies per sub-family (see
# _SRAM2_BASE_NS in the OEMiROT_Boot/Inc/region_defs.h of every STM32CubeU3
# board project).
#
# STM32U335 / STM32U345 have no entry on purpose: SRAM1 is only 16K there and
# ST's own STM32U345RCTXQ_FLASH.ld declares one flat 80K region, which is what
# the fallback below produces.
_U3_SRAM3 = ("RAM3", "xrw", 0x20040000, 320 * K, True)
_U3_SRAM4 = ("RAM4", "xrw", 0x20090000, 64 * K, True)
# Not an SRAMn bank and not part of the total RAM size; ST lists it in the
# MEMORY block but not in the header comment.
_U3_HSP = ("HSP_DATA_BRAM", "rw", 0x200A0000, 16 * K, False)

_U3X5_640K = [
    ("RAM", "xrw", 0x20000000, 192 * K, True),
    ("RAM2", "xrw", 0x20030000, 64 * K, True),
    _U3_SRAM3,
    _U3_SRAM4,
    _U3_HSP,
]

# Part of the total RAM of the device database that is deliberately left out of
# the MEMORY block, in bytes, keyed the same way as RAM_BANKS.
#
# STM32U0 has a backup SRAM directly behind SRAM1 (``BKPSRAM2_BASE`` in the
# CMSIS device headers).  It is contiguous, and the device database counts it
# in the RAM total, but it belongs to the backup domain and can be erased by
# hardware through SYSCFG_SCSR_SRAM2ER, so ST's STM32U031xx_FLASH.ld and
# STM32U083xx_FLASH.ld keep it out of the linker script.
RAM_EXCLUDED = {
    "STM32U031": 4 * K,  # 12K total, 8K SRAM1
    "STM32U073": 8 * K,  # 40K total, 32K SRAM1
    "STM32U083": 8 * K,  # 40K total, 32K SRAM1
}

RAM_BANKS = {
    # SRAM1 = 128K, SRAM2 right behind it (STM32U366RETXQ_FLASH.ld)
    "STM32U356": [
        ("RAM", "xrw", 0x20000000, 128 * K, True),
        ("RAM2", "xrw", 0x20020000, 64 * K, True),
    ],
    "STM32U366": [
        ("RAM", "xrw", 0x20000000, 128 * K, True),
        ("RAM2", "xrw", 0x20020000, 64 * K, True),
    ],
    # SRAM1 = 192K (STM32U375xx_FLASH.ld, STM32U385RGTXQ_FLASH.ld)
    "STM32U375": [
        ("RAM", "xrw", 0x20000000, 192 * K, True),
        ("RAM2", "xrw", 0x20030000, 64 * K, True),
    ],
    "STM32U385": [
        ("RAM", "xrw", 0x20000000, 192 * K, True),
        ("RAM2", "xrw", 0x20030000, 64 * K, True),
    ],
    # STM32U3C5ZITXQ_FLASH.ld, stm32u3b5xi_flash.ld
    "STM32U3B5": _U3X5_640K,
    "STM32U3C5": _U3X5_640K,
}

SIZE_PREFIX = "**                      "


def find_targets_xml(explicit=None):
    if explicit:
        return explicit
    matches = sorted(glob.glob(TARGETS_GLOB))
    if not matches:
        sys.exit("Cannot find stm32targets.xml, pass one with --targets")
    return matches[-1]


def text(element, tag):
    child = element.find(NS + tag)
    return child.text if child is not None else None


def read_devices(targets_xml, family, conf):
    """Return [(part number, flash bytes, total RAM bytes)] of one family."""
    devices = []
    for mcu in ET.parse(targets_xml).getroot().iter(NS + "mcu"):
        if text(mcu, "parent") != family:
            continue
        name = text(mcu, "name")
        flash = ram = None
        for memory in mcu.iter(NS + "memory"):
            size = int(text(memory, "size"), 16)
            if text(memory, "type") == "ROM":
                flash = size if flash is None else max(flash, size)
            else:
                ram = size if ram is None else ram + size
        if ram is None or (flash is None and conf.style != "default"):
            sys.exit("%s has no flash or RAM in %s" % (name, targets_xml))
        devices.append((name, flash, ram))
    if not devices:
        sys.exit("No device of %s in %s" % (family, targets_xml))
    return sorted(devices)


def ram_regions(part, ram_total):
    usable = ram_total - RAM_EXCLUDED.get(part[:9], 0)
    banks = RAM_BANKS.get(part[:9])
    if banks is None:
        return [("RAM", "xrw", 0x20000000, usable, True)]
    banked = sum(size for _, _, _, size, in_header in banks if in_header)
    if banked != usable:
        sys.exit(
            "%s: RAM_BANKS total %dK does not match the %dK of stm32targets.xml"
            % (part, banked // K, usable // K)
        )
    return banks


def size_str(nbytes):
    return "%dK" % (nbytes // K)


def render(template, part, series, flash, regions, stack_limit):
    sizes = [SIZE_PREFIX + "%sBytes FLASH" % size_str(flash)]
    for name, _, _, size, in_header in regions:
        if in_header:
            sizes.append(SIZE_PREFIX + "%sBytes %s" % (size_str(size), name))

    memory = [
        "  %s    (%s)    : ORIGIN = 0x%08X,   LENGTH = %s"
        % (name, attrs, origin, size_str(size))
        for name, attrs, origin, size, _ in regions
    ]
    memory.append("  FLASH    (rx)    : ORIGIN = 0x08000000,   LENGTH = %s" % size_str(flash))

    stack = [STACK_TOP] + ([STACK_LIMIT] if stack_limit else [])

    return string.Template(template).substitute(
        device=part,
        series=series,
        sizes="\n".join(sizes),
        stack="\n".join(stack),
        memory="\n".join(memory),
    )


def render_device(template, part):
    return string.Template(template).substitute(device=part[:9].upper())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("families", nargs="+", choices=sorted(FAMILIES))
    parser.add_argument("--targets", help="path to stm32targets.xml")
    parser.add_argument(
        "--outdir",
        default=REPO_DIR,
        help="where to put the <family>/ directories (default: the repository)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    targets_xml = find_targets_xml(args.targets)
    print("device database: %s" % targets_xml)

    for family in args.families:
        conf = FAMILIES[family]
        # newline="" keeps the line endings of the template; not every family
        # uses the same ones and rewriting them would churn the diff.
        with open(os.path.join(TEMPLATES_DIR, conf.template), newline="") as handle:
            template = handle.read()
        eol = "\r\n" if "\r\n" in template else "\n"
        template = template.replace("\r\n", "\n")

        scripts = {}
        for part, flash, ram_total in read_devices(targets_xml, family, conf):
            if conf.style == "cubeide":
                name = part.upper() + "_FLASH.ld"
                content = render(
                    template,
                    part,
                    family.upper(),
                    flash,
                    ram_regions(part, ram_total),
                    conf.stack_limit,
                )
            else:
                name = part[:11].upper() + "_DEFAULT.ld"
                content = render_device(template, part)
            if scripts.setdefault(name, content) != content:
                sys.exit("%s: %s does not match the other parts it covers" % (part, name))

        if not args.dry_run:
            family_dir = os.path.join(args.outdir, family)
            os.makedirs(family_dir, exist_ok=True)
            for name, content in sorted(scripts.items()):
                with open(os.path.join(family_dir, name), "w", newline="") as handle:
                    handle.write(content.replace("\n", eol))
        print(
            "%s: %d script(s)%s"
            % (family, len(scripts), " (dry run)" if args.dry_run else "")
        )


if __name__ == "__main__":
    main()
