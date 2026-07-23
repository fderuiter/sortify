"""Verification engine for proactive move validation."""

import os

from app.core.link_manager import LinkManager

try:
    import pylnk3
except ImportError:
    pylnk3 = None


def is_ml_available() -> bool:
    """Check if heavy machine learning dependencies (torch, easyocr) are available."""
    try:
        import easyocr  # noqa: F401
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


class VerificationEngine:
    """Engine to verify file operations before execution."""

    @staticmethod
    def verify_plan_integrity(base_dir: str, plan: dict) -> dict:
        """Run complete virtual filesystem simulation and integrity check."""
        tracker = VirtualFilesystemTracker()
        return tracker.verify_integrity(base_dir, plan)

    @staticmethod
    def get_moves(base_dir: str, plan: dict, current_dest: str = "") -> list:
        """Get a flat list of moves from the plan."""
        moves = []
        for key, content in plan.items():
            if content is None or (
                isinstance(content, dict)
                and content.get("__type__") in ("file", "directory")
            ):
                if isinstance(content, dict) and content.get("__type__") == "directory":
                    continue

                source_path = os.path.join(base_dir, key)

                if isinstance(content, dict) and "target_filename" in content:
                    filename = content["target_filename"]
                else:
                    filename = os.path.basename(key)

                dest_dir = os.path.join(base_dir, current_dest)
                dest_path = os.path.join(dest_dir, filename)
                moves.append((key, source_path, dest_path))
            else:
                moves.extend(
                    VerificationEngine.get_moves(
                        base_dir, content, os.path.join(current_dest, key)
                    )
                )
        return moves


class VirtualNode:
    """Represents a simulated file or directory in the virtual filesystem tracker."""

    def __init__(
        self, path, is_dir, inode, size, symlink_target=None, shortcut_target=None
    ):
        self.path = os.path.abspath(path)
        self.is_dir = is_dir
        self.inode = inode
        self.size = size
        self.symlink_target = symlink_target
        self.shortcut_target = shortcut_target


class VirtualFilesystemTracker:
    """In-memory virtual filesystem tracker to simulate file operations and verify path integrity."""

    def __init__(self):
        self.nodes = {}
        self._inode_counter = 1000000

    def _get_next_inode(self) -> int:
        self._inode_counter += 1
        return self._inode_counter

    def populate_from_disk(self, base_dir: str):
        """Traverse base_dir recursively and populate the virtual filesystem."""
        if not base_dir or not os.path.exists(base_dir):
            return

        base_dir_abs = os.path.abspath(base_dir)

        # Add base_dir itself
        try:
            st = os.stat(base_dir_abs)
            inode = st.st_ino
            size = st.st_size
        except OSError:
            inode = self._get_next_inode()
            size = 0
        self.nodes[base_dir_abs] = VirtualNode(
            base_dir_abs, is_dir=True, inode=inode, size=size
        )

        for root, dirs, files in os.walk(base_dir):
            for d in dirs:
                dir_path = os.path.join(root, d)
                abs_dir_path = os.path.abspath(dir_path)
                try:
                    st = os.stat(abs_dir_path)
                    inode = st.st_ino
                    size = st.st_size
                except OSError:
                    inode = self._get_next_inode()
                    size = 0
                self.nodes[abs_dir_path] = VirtualNode(
                    abs_dir_path, is_dir=True, inode=inode, size=size
                )

            for f in files:
                file_path = os.path.join(root, f)
                abs_file_path = os.path.abspath(file_path)

                # Check if symbolic link
                symlink_target = None
                if os.path.islink(abs_file_path):
                    try:
                        symlink_target = os.readlink(abs_file_path)
                    except OSError:
                        pass

                # Check if Windows shortcut (.lnk)
                shortcut_target = None
                if abs_file_path.lower().endswith(".lnk"):
                    info = LinkManager.get_link_info(abs_file_path)
                    if info and info.get("type") == "lnk":
                        shortcut_target = info.get("target")
                    elif pylnk3:
                        try:
                            lnk = pylnk3.parse(abs_file_path)
                            shortcut_target = lnk.path
                        except Exception:
                            pass

                try:
                    st = (
                        os.lstat(abs_file_path)
                        if symlink_target
                        else os.stat(abs_file_path)
                    )
                    inode = st.st_ino
                    size = st.st_size
                except OSError:
                    inode = self._get_next_inode()
                    size = 0

                self.nodes[abs_file_path] = VirtualNode(
                    abs_file_path,
                    is_dir=False,
                    inode=inode,
                    size=size,
                    symlink_target=symlink_target,
                    shortcut_target=shortcut_target,
                )

    def populate_from_moves(self, moves_list: list):
        """Ensure all source files in the moves are registered in the virtual filesystem."""
        for rel_path, src, dst in moves_list:
            abs_src = os.path.abspath(src)
            if abs_src not in self.nodes:
                inode = self._get_next_inode()
                symlink_target = None
                shortcut_target = None

                # Retrieve registered link info if available
                link_info = LinkManager.get_link_info(abs_src)
                if link_info:
                    if link_info.get("type") == "symlink":
                        symlink_target = link_info.get("target")
                    elif link_info.get("type") == "lnk":
                        shortcut_target = link_info.get("target")
                else:
                    if abs_src.lower().endswith(".lnk"):
                        shortcut_target = "mock_target.exe"

                self.nodes[abs_src] = VirtualNode(
                    abs_src,
                    is_dir=False,
                    inode=inode,
                    size=1024,  # default mock size
                    symlink_target=symlink_target,
                    shortcut_target=shortcut_target,
                )

    def simulate_final_state(self, moves_list: list) -> dict:
        """Return a new dict of nodes representing the filesystem state after executing all moves."""
        final_nodes = {}

        # 1. Start with copy of unmoved nodes
        moved_srcs = {os.path.abspath(src) for _, src, _ in moves_list}
        for path, node in self.nodes.items():
            if path not in moved_srcs:
                final_nodes[path] = node

        # 2. Add moved nodes at their destinations
        for rel_path, src, dst in moves_list:
            abs_src = os.path.abspath(src)
            abs_dst = os.path.abspath(dst)
            node = self.nodes.get(abs_src)
            if node:
                new_node = VirtualNode(
                    abs_dst,
                    is_dir=node.is_dir,
                    inode=node.inode,
                    size=node.size,
                    symlink_target=node.symlink_target,
                    shortcut_target=node.shortcut_target,
                )
                final_nodes[abs_dst] = new_node

                # Also ensure all intermediate parent directories are in the final_nodes
                # unless they are already occupied by a file.
                current = os.path.dirname(abs_dst)
                while current and len(current) > 0:
                    if current not in final_nodes:
                        final_nodes[current] = VirtualNode(
                            current, is_dir=True, inode=self._get_next_inode(), size=0
                        )
                    parent = os.path.dirname(current)
                    if parent == current:
                        break
                    current = parent

        return final_nodes

    def check_collisions(self, moves_list: list, base_dir: str) -> list:
        """Detect duplicate destination target collisions and blocked parent directory/ancestor paths."""
        collisions = []
        base_dir_abs = os.path.abspath(base_dir) if base_dir else ""

        dest_to_srcs = {}
        for rel_path, src, dst in moves_list:
            abs_src = os.path.abspath(src)
            abs_dst = os.path.abspath(dst)
            dest_to_srcs.setdefault(abs_dst, []).append(abs_src)

        # 1. Duplicate target collision
        for dst, srcs in dest_to_srcs.items():
            if len(srcs) > 1:
                collisions.append(
                    {
                        "path": dst,
                        "type": "duplicate_target",
                        "sources": srcs,
                        "message": f"Multiple files are planned to be moved to the same destination: {dst}",
                    }
                )

        # 2. Parent directory / target structure collisions
        for rel_path, src, dst in moves_list:
            abs_src = os.path.abspath(src)
            abs_dst = os.path.abspath(dst)

            # Check if any ancestor is a file (blocked parent directory)
            current = os.path.dirname(abs_dst)
            ancestor_collision = False
            while current and len(current) >= len(base_dir_abs):
                node = self.nodes.get(current)
                if node and not node.is_dir:
                    collisions.append(
                        {
                            "path": abs_dst,
                            "type": "parent_directory_collision",
                            "blocked_by": current,
                            "source": abs_src,
                            "message": f"Destination path '{abs_dst}' is blocked because ancestor '{current}' is a file.",
                        }
                    )
                    ancestor_collision = True
                    break
                parent = os.path.dirname(current)
                if parent == current:
                    break
                current = parent

            if ancestor_collision:
                continue

            # Check destination itself
            node_at_dst = self.nodes.get(abs_dst)
            if node_at_dst:
                if node_at_dst.is_dir:
                    collisions.append(
                        {
                            "path": abs_dst,
                            "type": "target_is_directory",
                            "source": abs_src,
                            "message": f"Destination path '{abs_dst}' is already an existing directory.",
                        }
                    )
                else:
                    is_dst_moving = any(
                        os.path.abspath(s) == abs_dst for _, s, _ in moves_list
                    )
                    if not is_dst_moving and abs_src != abs_dst:
                        collisions.append(
                            {
                                "path": abs_dst,
                                "type": "target_collision",
                                "source": abs_src,
                                "message": f"Destination path '{abs_dst}' is blocked by an existing file that is not being moved.",
                            }
                        )

        return collisions

    def check_circular_renames(self, moves_list: list) -> list:
        """Trace renaming dependencies and detect circular renames."""
        move_map = {}
        for rel_path, src, dst in moves_list:
            abs_src = os.path.abspath(src)
            abs_dst = os.path.abspath(dst)
            if abs_src != abs_dst:
                move_map[abs_src] = abs_dst

        cycles = []
        visited = set()

        for start_path in move_map:
            if start_path in visited:
                continue

            path = start_path
            current_chain = []
            chain_set = set()

            while path in move_map:
                if path in chain_set:
                    cycle_start_idx = current_chain.index(path)
                    cycle = current_chain[cycle_start_idx:]
                    cycles.append(cycle)
                    break
                if path in visited:
                    break

                current_chain.append(path)
                chain_set.add(path)
                path = move_map[path]

            for p in current_chain:
                visited.add(p)

        circular_renames = []
        for cycle in cycles:
            cycle_str = " -> ".join(cycle) + f" -> {cycle[0]}"
            message = f"Circular renaming dependency detected: {cycle_str}"
            for path in cycle:
                circular_renames.append(
                    {"path": path, "cycle": cycle, "message": message}
                )

        return circular_renames

    def check_broken_links(
        self, moves_list: list, base_dir: str, final_nodes: dict
    ) -> list:
        """Validate symbolic links and Windows shortcut targets after simulating moves."""
        broken_links = []
        base_dir_abs = os.path.abspath(base_dir) if base_dir else ""

        path_map = {
            os.path.abspath(src): os.path.abspath(dst) for _, src, dst in moves_list
        }

        for abs_path, node in final_nodes.items():
            target_path = None
            link_type = None

            if node.symlink_target:
                target_path = node.symlink_target
                link_type = "symlink"
            elif node.shortcut_target:
                target_path = node.shortcut_target
                link_type = "shortcut"

            if not target_path:
                continue

            # Determine original absolute target path before moves
            orig_src = None
            for rel_path, src, dst in moves_list:
                if os.path.abspath(dst) == abs_path:
                    orig_src = os.path.abspath(src)
                    break

            if orig_src is None:
                orig_src = abs_path

            if os.path.isabs(target_path):
                abs_target_orig = os.path.abspath(target_path)
            else:
                abs_target_orig = os.path.abspath(
                    os.path.join(os.path.dirname(orig_src), target_path)
                )

            # Final absolute target path after moves
            abs_target_final = path_map.get(abs_target_orig, abs_target_orig)

            # Verify target existence
            is_inside_base = (
                abs_target_final.startswith(base_dir_abs) if base_dir_abs else False
            )

            if is_inside_base:
                target_exists = abs_target_final in final_nodes
            else:
                target_exists = os.path.exists(abs_target_final)

            if not target_exists:
                message = f"Broken {link_type} target: '{abs_path}' points to '{abs_target_final}', which does not exist."
                broken_links.append(
                    {
                        "path": abs_path,
                        "type": f"broken_{link_type}",
                        "target": abs_target_final,
                        "message": message,
                    }
                )

        return broken_links

    def verify_integrity(self, base_dir: str, plan: dict) -> dict:
        """Run complete virtual filesystem simulation and integrity check."""
        moves_list = VerificationEngine.get_moves(base_dir, plan)

        # Populate initial VFS state
        self.populate_from_disk(base_dir)
        self.populate_from_moves(moves_list)

        # Trace and analyze issues
        collisions = self.check_collisions(moves_list, base_dir)
        circular_renames = self.check_circular_renames(moves_list)

        # Simulate final VFS state
        final_nodes = self.simulate_final_state(moves_list)

        # Validate links in final VFS state
        broken_links = self.check_broken_links(moves_list, base_dir, final_nodes)

        # Consolidate all warnings
        warnings = []
        for c in collisions:
            warnings.append(c["message"])
        for r in circular_renames:
            warnings.append(r["message"])
        for link in broken_links:
            warnings.append(link["message"])

        # Eliminate duplicate messages in warnings
        unique_warnings = list(dict.fromkeys(warnings))

        success = (
            len(collisions) == 0
            and len(circular_renames) == 0
            and len(broken_links) == 0
        )

        return {
            "success": success,
            "collisions": collisions,
            "circular_renames": circular_renames,
            "broken_links": broken_links,
            "warnings": unique_warnings,
        }
