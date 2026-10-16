#!/usr/bin/env python3
"""Static PE (Portable Executable) Binary Dependency Verification Script.

Inspects compiled Windows binaries (.exe, .dll, .pyd) in the target output bundle
directory to verify that every dynamic library dependency required by the executable
and native components is either a standard Windows system DLL or present in the bundle.
"""

import argparse
import sys
from pathlib import Path
from typing import List, Set, Tuple

import pefile

# Whitelist of standard Windows OS system DLLs present on clean Windows installations
STANDARD_SYSTEM_DLLS: Set[str] = {
    "kernel32.dll",
    "kernelbase.dll",
    "user32.dll",
    "gdi32.dll",
    "gdi32full.dll",
    "win32u.dll",
    "advapi32.dll",
    "shell32.dll",
    "shlwapi.dll",
    "ole32.dll",
    "oleaut32.dll",
    "combase.dll",
    "comctl32.dll",
    "ws2_32.dll",
    "rpcrt4.dll",
    "version.dll",
    "ntdll.dll",
    "sechost.dll",
    "msvcrt.dll",
    "bcrypt.dll",
    "crypt32.dll",
    "imm32.dll",
    "setupapi.dll",
    "iphlpapi.dll",
    "userenv.dll",
    "dwmapi.dll",
    "uxtheme.dll",
    "winmm.dll",
    "wsock32.dll",
    "dnsapi.dll",
    "mpr.dll",
    "netapi32.dll",
    "psapi.dll",
    "wintrust.dll",
    "cryptbase.dll",
    "sspicli.dll",
    "profapi.dll",
    "powrprof.dll",
    "cfgmgr32.dll",
    "devobj.dll",
    "wtsapi32.dll",
    "urlmon.dll",
    "wininet.dll",
    "normaliz.dll",
    "msi.dll",
    "bcryptprimitives.dll",
    "ncrypt.dll",
    "d3d11.dll",
    "dxgi.dll",
    "opengl32.dll",
    "apphelp.dll",
    "cryptsp.dll",
    "rsaenh.dll",
    "imagehlp.dll",
    "dbghelp.dll",
    "sfc.dll",
    "sfc_os.dll",
    "propsys.dll",
    "clbcatq.dll",
    "mswsock.dll",
    "dhcpcsvc.dll",
    "secur32.dll",
    "authz.dll",
    "schannel.dll",
    "cryptnet.dll",
    "sensapi.dll",
    "credui.dll",
    "gpapi.dll",
    "cabinet.dll",
}


def is_system_dll(dll_name: str) -> bool:
    """Check if a DLL name matches standard Windows system whitelist or OS API sets."""
    name_lower = dll_name.lower()
    if name_lower in STANDARD_SYSTEM_DLLS:
        return True
    if name_lower.startswith("api-ms-win-") or name_lower.startswith("ext-ms-win-"):
        return True
    return False


def extract_pe_imports(pe_path: Path) -> List[str]:
    """Parse PE header and return list of imported DLL names."""
    try:
        with open(pe_path, "rb") as f:
            magic = f.read(2)
        if magic != b"MZ":
            # Not a valid PE binary (e.g. mock/text stub)
            return []

        pe = pefile.PE(str(pe_path), fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
        )
        imports = []
        if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
            for entry in pe.DIRECTORY_ENTRY_IMPORT:
                if entry.dll:
                    dll_str = entry.dll.decode("utf-8", errors="ignore").strip().lower()
                    if dll_str:
                        imports.append(dll_str)
        pe.close()
        return imports
    except Exception as e:
        print(
            f"Warning: Failed to parse PE imports for {pe_path}: {e}", file=sys.stderr
        )
        return []


def verify_pe_dependencies(
    bundle_dir: Path, allow_empty: bool = False, verbose: bool = False
) -> Tuple[bool, List[Tuple[str, str]]]:
    """Scan all PE binaries in bundle_dir and verify dependencies against whitelist and bundled files."""
    if not bundle_dir.exists():
        if allow_empty:
            print(f"Bundle directory {bundle_dir} does not exist. Skipping PE check.")
            return True, []
        print(f"Error: Bundle directory not found at {bundle_dir}", file=sys.stderr)
        return False, [("BUNDLE_DIR", f"Directory not found: {bundle_dir}")]

    # Gather all bundled filenames (lowercased)
    bundled_files: Set[str] = {
        f.name.lower() for f in bundle_dir.rglob("*") if f.is_file()
    }

    # Gather PE binary candidates (.exe, .dll, .pyd)
    pe_candidates = sorted(
        [
            f
            for f in bundle_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in (".exe", ".dll", ".pyd")
        ]
    )

    if not pe_candidates:
        if allow_empty:
            print(f"No PE binaries found in {bundle_dir}. Skipping PE check.")
            return True, []
        print(f"Error: No PE binaries found in {bundle_dir}", file=sys.stderr)
        return False, [("BUNDLE_DIR", f"No PE binaries found in {bundle_dir}")]

    missing_deps: List[Tuple[str, str]] = []
    total_checks = 0

    for pe_file in pe_candidates:
        rel_pe_path = pe_file.relative_to(bundle_dir).as_posix()
        imported_dlls = extract_pe_imports(pe_file)

        if verbose:
            print(f"Inspecting {rel_pe_path} ({len(imported_dlls)} imported DLLs)...")

        for dll in imported_dlls:
            total_checks += 1
            if is_system_dll(dll):
                continue
            if dll in bundled_files:
                continue

            missing_deps.append((rel_pe_path, dll))

    success = len(missing_deps) == 0
    if success:
        print(
            f"[PE VERIFICATION PASS] Successfully verified {len(pe_candidates)} PE binaries across {total_checks} dynamic imports in {bundle_dir}. Zero missing dynamic dependencies."
        )
    else:
        print(
            f"\n=== PE DEPENDENCY VERIFICATION FAILURE: Missing dynamic dependencies in {bundle_dir} ===",
            file=sys.stderr,
        )
        seen = set()
        for rel_path, dll in missing_deps:
            key = (rel_path, dll)
            if key in seen:
                continue
            seen.add(key)
            print(
                f"  - Binary '{rel_path}' depends on missing dynamic library '{dll}'",
                file=sys.stderr,
            )
        print(
            "\nPlease ensure all Visual C++ and Universal C Runtime libraries are dynamically harvested during packaging.",
            file=sys.stderr,
        )

    return success, missing_deps


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that all PE binary dynamic dependencies exist in the bundle or system whitelist."
    )
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        default=Path("dist") / "smart-autosorter",
        help="Path to executable bundle output directory",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Do not fail if bundle directory or PE binaries are absent",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed binary inspection logs",
    )
    args = parser.parse_args()

    success, _ = verify_pe_dependencies(
        args.bundle_dir, allow_empty=args.allow_empty, verbose=args.verbose
    )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
