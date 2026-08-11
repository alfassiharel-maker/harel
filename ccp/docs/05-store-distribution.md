# Windows Store Distribution: MSIX Packaging and Publication

CCP Forge is packaged as an MSIX (Microsoft Store Package) for distribution through the Microsoft Store. This document describes the packaging configuration and publication process.

## What is MSIX?

MSIX is Microsoft's modern application packaging format for Windows 10 and later. It provides:

- **Signed distribution**: packages are signed with a certificate, ensuring authenticity
- **Automatic updates**: the Store handles version upgrades
- **Isolated installation**: each user gets their own copy, no admin required
- **Clean uninstall**: all registry and file changes are reversible
- **Store discovery**: customers find the app through Microsoft Search

## Build Environment Requirements

Building MSIX requires:

- **Windows 10 or Windows 11** (MSIX building is Windows-only)
- **Visual Studio Build Tools** or full Visual Studio
- **Signing certificate** (self-signed for testing, official for Store)

**Note**: MSIX building cannot happen on Linux or macOS. The single-file executable is built with PyInstaller on any platform, but MSIX packaging requires Windows-specific tools.

## Project Structure

```
packaging/windows/
├── Package.appxmanifest          # MSIX package metadata
├── build-msix.ps1               # PowerShell build script
└── AppxPackagingTool.exe         # Windows App Packaging Tool
```

## Package.appxmanifest

The manifest declares application identity, capabilities, entry point, and visual assets.

```xml
<?xml version="1.0" encoding="utf-8"?>
<Package
    xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
    xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
    xmlns:uap="http://schemas.microsoft.com/appx/uap/windows10"
    IgnorableNamespaces="uap rescap">

    <Identity
        Name="CCPForgeConsumer.HarelMaker"
        Publisher="CN=HarelMaker"
        Version="1.0.0.0" />

    <Properties>
        <DisplayName>CCP Forge</DisplayName>
        <PublisherDisplayName>Harel Maker</PublisherDisplayName>
        <Logo>Assets/Store/StoreLogo.png</Logo>
    </Properties>

    <Dependencies>
        <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.17763.0" MaxVersionTested="10.0.19041.0" />
    </Dependencies>

    <Resources>
        <Resource Language="en-us" />
    </Resources>

    <Applications>
        <Application
            Id="CCPForge"
            StartPage="ccp/ui/launcher.py"
            EntryPoint="ccp.ui.__main__">
            <uap:VisualElements
                DisplayName="CCP Forge"
                Square150x150Logo="Assets/Store/Square150x150.png"
                Square44x44Logo="Assets/Store/Square44x44.png"
                Description="Find shared structure in files, store one copy, read parts without rebuilding."
                BackgroundColor="#FFFFFF">
                <uap:SplashScreen Image="Assets/Store/Splash.png" />
            </uap:VisualElements>
        </Application>
    </Applications>

    <Capabilities>
        <!-- File system access for user documents and downloads -->
        <Capability Name="documentsLibrary" />
        <Capability Name="picturesLibrary" />
        <Capability Name="videosLibrary" />
        <rescap:Capability Name="broadFileSystemAccess" />
    </Capabilities>
</Package>
```

Key fields:
- `Identity/Name`: unique package identifier (must be registered in Partner Center)
- `Identity/Publisher`: signing certificate subject (CN=...)
- `Identity/Version`: semantic version (major.minor.build.revision)
- `DisplayName`: user-visible name in Start menu
- `PublisherDisplayName`: company name
- `StartPage` / `EntryPoint`: Python entry point (the launcher script)
- `Capabilities`: file system, network, device permissions

## Build Process

### 1. Create Visual Assets

MSIX requires PNG images for the Store and Start menu:

- `Square44x44.png`: app icon (44×44 px)
- `Square150x150.png`: Start menu tile (150×150 px)
- `StoreLogo.png`: Store listing icon (50×50 px)
- `Splash.png`: splash screen on launch (620×300 px)

Place in `packaging/windows/Assets/Store/`.

### 2. Build the Executable

```bash
python3 packaging/build_app.py
```

This produces `dist/CCPForge.exe` (standalone executable with embedded Python and dependencies).

### 3. Create MSIX Package

Run the PowerShell script (on Windows):

```powershell
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope CurrentUser
.\packaging\windows\build-msix.ps1 -Version "1.0.0.0"
```

This script:
1. Validates `Package.appxmanifest`
2. Stages the executable and dependencies
3. Creates a signing certificate (for testing)
4. Builds the `.appx` package
5. Signs it with the certificate
6. Outputs to `dist/CCPForge_1.0.0.0_x64.appx`

### 4. Test the Package

```powershell
# Add-AppxPackage requires the certificate to be trusted
Import-Certificate -FilePath cert.cer -CertStoreLocation Cert:\CurrentUser\TrustedPeople

# Install
Add-AppxPackage dist/CCPForge_1.0.0.0_x64.appx

# Test
Start-AppxPackage -Name "CCPForgeConsumer.HarelMaker"

# Uninstall
Remove-AppxPackage "CCPForgeConsumer.HarelMaker_1.0.0.0_x64_en-us"
```

## Publishing to Microsoft Store

### Prerequisites

1. **Microsoft Partner Center Account**: developer account ($19 one-time)
2. **App Registered**: register "CCP Forge" and claim the package name
3. **Store Listing**: write description, upload screenshots, set pricing
4. **Privacy Policy**: hosted publicly (e.g., on a website)
5. **Code Signing Certificate**: issued by an authorized CA (not self-signed)

### Process

1. **Log in to Partner Center**: https://partner.microsoft.com/dashboard
2. **Create New App**: "CCP Forge", claim package name "CCPForgeConsumer.HarelMaker"
3. **Product Setup**:
   - Pricing and availability: "Free" or "$X.XX"
   - Properties: category "Utilities", age rating, system requirements
   - Packages: upload `CCPForge_1.0.0.0_x64.appx`
4. **Store Listing**: upload screenshots, videos, description, keywords
5. **Submission**: submit for certification (48–72 hours typical)
6. **Certification**: Microsoft reviews for malware, crashes, compliance
7. **Publication**: after approval, app is live in the Store

### Store Listing Template

```
Title: CCP Forge

Short description:
Find shared structure in files. Store one copy. Read parts without rebuilding.

Full description:
CCP Forge takes your project files and discovers their shared structure.
Instead of storing each file separately, it stores one shared "base" and
records what makes each file unique. The result is a compact artifact you
can browse, read from selectively (without rebuilding), and verify.

Key features:
• Saves storage by finding duplicated code, data, and structure
• Selective reads: extract a 256-byte window without rebuilding the whole file
• Verification: confirm your artifact is intact before using it
• Export: extract any file back to its original bytes
• Cross-platform: works on Windows, macOS, and Linux

No dependencies. No cloud. Your data stays on your machine.

Requirements:
- Windows 10 (build 17763) or later
- 512 MB free disk space for artifacts
```

## Versioning and Updates

Each release increments the version in `Package.appxmanifest`:

```xml
<Identity
    Name="CCPForgeConsumer.HarelMaker"
    Version="1.1.0.0" />
```

Version format: `major.minor.build.revision`

When a new version is submitted to the Store:
1. Microsoft hosts it alongside the previous version
2. Users with the older version see an "update available" notification
3. Updates are automatic (unless the user disables auto-updates)

## Signing and Certificates

### For Testing (Self-Signed)

```powershell
# Create self-signed certificate
$cert = New-SelfSignedCertificate `
    -Type CodeSigningCert `
    -Subject "CN=CCPForgeTest" `
    -KeyUsage DigitalSignature `
    -KeyLength 2048 `
    -NotAfter (Get-Date).AddYears(5)

# Export for installation on other machines
Export-Certificate -Cert $cert -FilePath "cert.cer"
```

Self-signed certificates work for local testing and internal distribution, but the Store requires an official certificate.

### For Store Distribution (Official Certificate)

Obtain a code-signing certificate from:
- DigiCert
- GlobalSign
- Sectigo (formerly Comodo)
- Entrust

Cost: $200–$400/year typically.

Then sign the MSIX:

```powershell
SignTool sign /f cert.pfx /fd SHA256 /t http://timestamp.server.example.com `
    dist/CCPForge_1.0.0.0_x64.appx
```

## Compliance and Privacy

### Privacy Policy

Microsoft requires a privacy policy accessible at a public URL. Example:

```
https://www.example.com/privacy/ccp-forge
```

The policy must disclose:
- What data is collected (none, in this case — everything is local)
- How data is used
- How users can contact you with questions
- When/how data is deleted

CCP Forge collects nothing. A minimal policy might state:

> CCP Forge runs entirely on your machine. No data is sent to the internet.
> No analytics, no crash reporting, no telemetry. Your projects stay private.

### Accessibility

Microsoft expects accessible apps:
- High-contrast mode support
- Keyboard navigation
- Screen reader compatibility

CCP Forge's browser-based UI inherits browser accessibility. Test with:
- Windows High Contrast Mode (Windows + U)
- Keyboard-only navigation (Tab, Enter, arrow keys)
- Narrator (Windows + Ctrl + N)

### Content Policies

The Store prohibits:
- Malware, adware, or spyware
- Deceptive functionality
- Explicit sexual or violent content
- Hate speech
- Misleading descriptions

CCP Forge complies by design: it's a straightforward utility with no deceptive behavior.

## Distribution Beyond the Store

CCP Forge can also be distributed outside the Store:

### Direct Download

Provide `dist/CCPForge.exe` on a website. Users download and run directly. No signing certificate required (though trusted certificates prevent "unknown publisher" warnings).

### Side-Loading (Enterprise)

IT administrators can install `.appx` packages on corporate machines:

```powershell
Add-AppxPackage -Path CCPForge_1.0.0.0_x64.appx
```

This requires the signing certificate to be trusted on the target machine.

### GitHub Releases

Publish executables as GitHub release artifacts. No store submission needed, but users must manually update.

## Known Limitations

1. **MSIX Isolation**: MSIX packages run in a virtualized file system. App data goes to:
   ```
   %LOCALAPPDATA%\Packages\CCPForgeConsumer.HarelMaker_...\LocalCache\
   ```
   Rather than the typical `%APPDATA%` directory.

2. **Python in MSIX**: Embedding Python in MSIX increases package size (~100 MB). The store allows this, but it's larger than a native binary.

3. **Auto-Update Behavior**: Store updates are automatic but users can disable them. There's no way to force updates.

4. **Certification Time**: the Store's certification process takes 48–72 hours. Plan releases accordingly.

5. **Version Numbering**: the Store's versioning is strictly `major.minor.build.revision` (four integers). Semantic versioning with pre-release tags (1.0.0-beta) is not supported.

## Release Checklist

- [ ] Increment version in `Package.appxmanifest`
- [ ] Test on Windows 10 and Windows 11
- [ ] Build `.exe` with `python3 packaging/build_app.py`
- [ ] Create `.appx` with `.\packaging\windows\build-msix.ps1`
- [ ] Test installation: `Add-AppxPackage dist/CCPForge_*.appx`
- [ ] Test all features (build, read, export, commercial)
- [ ] Update privacy policy (if applicable)
- [ ] Update store listing description/screenshots
- [ ] Submit to Microsoft Partner Center
- [ ] Monitor certification progress
- [ ] After approval, announce release
