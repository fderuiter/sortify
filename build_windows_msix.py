"""Script to build Windows MSIX package."""

import os
import subprocess

import tomllib


def get_version():
    """Extract version from pyproject.toml."""
    with open("pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    version = data.get("project", {}).get("version", "0.1.0")
    parts = version.split('.')
    while len(parts) < 4:
        parts.append('0')
    return ".".join(parts[:4])

def create_assets(target_dir):
    """Create necessary image assets for the MSIX package."""
    assets_dir = os.path.join(target_dir, "Assets")
    os.makedirs(assets_dir, exist_ok=True)
    
    try:
        from PIL import Image
    except ImportError:
        subprocess.run(["uv", "pip", "install", "pillow"], check=True)
        from PIL import Image

    for size, name in [
        ((44, 44), "Square44x44Logo.png"),
        ((150, 150), "Square150x150Logo.png"),
        ((50, 50), "StoreLogo.png"),
        ((620, 300), "SplashScreen.png"),
        ((44, 44), "AppIcon.png")
    ]:
        img = Image.new("RGBA", size, (255, 0, 0, 0))
        img.save(os.path.join(assets_dir, name))

def create_manifest(target_dir, version):
    """Generate the AppxManifest.xml for the MSIX package."""
    manifest_content = f"""<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
         xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
         xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities">
  <Identity Name="SmartAutoSorter"
            Publisher="CN=Smart AutoSorter"
            Version="{version}"
            ProcessorArchitecture="x64" />
  <Properties>
    <DisplayName>Smart AutoSorter AI Pro</DisplayName>
    <PublisherDisplayName>Smart AutoSorter</PublisherDisplayName>
    <Description>Smart AutoSorter AI Pro</Description>
    <Logo>Assets\\StoreLogo.png</Logo>
  </Properties>
  <Resources>
    <Resource Language="en-us" />
  </Resources>
  <Dependencies>
    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.17763.0" MaxVersionTested="10.0.19041.0" />
  </Dependencies>
  <Capabilities>
    <rescap:Capability Name="runFullTrust" />
  </Capabilities>
  <Applications>
    <Application Id="SmartAutoSorter"
                 Executable="smart-autosorter.exe"
                 EntryPoint="Windows.FullTrustApplication">
      <uap:VisualElements DisplayName="Smart AutoSorter AI Pro"
                          Description="Smart AutoSorter AI Pro"
                          BackgroundColor="transparent"
                          Square150x150Logo="Assets\\Square150x150Logo.png"
                          Square44x44Logo="Assets\\Square44x44Logo.png">
        <uap:SplashScreen Image="Assets\\SplashScreen.png" />
      </uap:VisualElements>
    </Application>
  </Applications>
</Package>
"""
    with open(os.path.join(target_dir, "AppxManifest.xml"), "w") as f:
        f.write(manifest_content)

def main():
    """Build the executable and prepare MSIX package assets."""
    version = get_version()
    # Ensure pyinstaller output folder exists
    subprocess.run(["uv", "run", "pyinstaller", "--noconfirm", "--windowed", "--name", "smart-autosorter", "app/main.py"], check=True)
    
    dist_dir = "dist/smart-autosorter"
    
    create_assets(dist_dir)
    create_manifest(dist_dir, version)
    
    # Save version to a file so bash/powershell can read it easily
    with open("build_version.txt", "w") as f:
        f.write(version)

if __name__ == "__main__":
    main()
