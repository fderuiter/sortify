import os
import subprocess
import sys
import tempfile


def test_tar_packaging_nested_and_long_paths():
    """
    Verify that native tar (tar.exe on Windows, tar on Unix) packages and extracts
    nested paths correctly, including paths that exceed 260 characters,
    retaining standard file attributes and permissions.
    """
    # Create a temporary directory structure
    with tempfile.TemporaryDirectory() as tmp_dir:
        dist_dir = os.path.join(tmp_dir, "dist")
        os.makedirs(dist_dir)

        # Create a deep nested path that exceeds 260 characters
        # E.g., dist/smart-autosorter/very/long/nested/path/.../file.txt
        app_name = "smart-autosorter"
        app_dir = os.path.join(dist_dir, app_name)
        os.makedirs(app_dir)

        # Build a deep path segments list to exceed 260 characters
        # 10 segments of 25 characters each = 250 characters + app_dir path
        segments = ["nested_dir_segment_name_long" for _ in range(10)]
        deep_dir = os.path.join(app_dir, *segments)
        os.makedirs(deep_dir, exist_ok=True)

        target_file = os.path.join(deep_dir, "test_file.txt")
        assert len(target_file) > 260, (
            f"Path length is {len(target_file)}, should be > 260"
        )

        # Write dummy file content
        file_content = "This is a test file packed in a deep directory structure."
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(file_content)

        # Make another file with executable permissions if on Unix-like systems
        exec_file = os.path.join(app_dir, "run_app.sh")
        with open(exec_file, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho 'running'")

        # Set executable permissions on run_app.sh
        if sys.platform != "win32":
            os.chmod(exec_file, 0o755)
            initial_mode = os.stat(exec_file).st_mode

        # Package file name
        archive_name = (
            "test-archive.zip" if sys.platform == "win32" else "test-archive.tar.gz"
        )
        archive_path = os.path.join(dist_dir, archive_name)

        # Use tar.exe on Windows, tar on others
        tar_bin = "tar.exe" if sys.platform == "win32" else "tar"

        # Prepare tar command exactly like in release.yml:
        # tar.exe -acf dist\${{ matrix.artifact_name }} -C dist smart-autosorter
        # tar -czf dist/${{ matrix.artifact_name }} -C dist smart-autosorter
        if sys.platform == "win32":
            cmd = [tar_bin, "-acf", archive_path, "-C", dist_dir, app_name]
        else:
            cmd = [tar_bin, "-czf", archive_path, "-C", dist_dir, app_name]

        # Execute tar command
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0, (
            f"Tar packaging failed: {res.stderr}\nStdout: {res.stdout}"
        )

        # Verify archive was created
        assert os.path.exists(archive_path), "Archive file was not created"

        # Now extract the archive in a clean directory
        extract_dir = os.path.join(tmp_dir, "extracted")
        os.makedirs(extract_dir)

        if sys.platform == "win32":
            # Extract zip file using tar.exe
            # tar.exe -xf archive_path -C extract_dir
            extract_cmd = [tar_bin, "-xf", archive_path, "-C", extract_dir]
        else:
            # Extract tar.gz using tar
            extract_cmd = [tar_bin, "-xzf", archive_path, "-C", extract_dir]

        res_extract = subprocess.run(extract_cmd, capture_output=True, text=True)
        assert res_extract.returncode == 0, (
            f"Extraction failed: {res_extract.stderr}\nStdout: {res_extract.stdout}"
        )

        # Verify extracted files
        extracted_app_dir = os.path.join(extract_dir, app_name)
        assert os.path.exists(extracted_app_dir), "Extracted app folder does not exist"

        extracted_deep_file = os.path.join(
            extracted_app_dir, *segments, "test_file.txt"
        )
        assert os.path.exists(extracted_deep_file), (
            f"Extracted deep file not found at {extracted_deep_file}"
        )

        with open(extracted_deep_file, "r", encoding="utf-8") as f:
            content = f.read()
        assert content == file_content, "Extracted file content mismatch"

        # Verify executable permissions are preserved on Unix-like systems
        if sys.platform != "win32":
            extracted_exec_file = os.path.join(extracted_app_dir, "run_app.sh")
            assert os.path.exists(extracted_exec_file)
            extracted_mode = os.stat(extracted_exec_file).st_mode
            assert (extracted_mode & 0o111) == (initial_mode & 0o111), (
                "Executable permissions were not preserved"
            )
