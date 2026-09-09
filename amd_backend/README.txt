DLSS5Tool AMD developer test — user-supplied components only

This folder is intentionally empty of the third-party DLSS-NR runtime.

1. Read https://github.com/danielblnc/DLSS-NR-on-AMD/blob/master/LICENSE
2. Download dlssnr_on_amd_setup.exe from its official releases:
   https://github.com/danielblnc/DLSS-NR-on-AMD/releases
3. Place the installer and your legitimately obtained nvngx_dlssnr.dll
   (310.8.0.0) in this folder. Run the official installer yourself, or confirm
   the action in AMD-DevTest.exe. Do not bypass security software.
4. The installer must generate version.dll and dlssnr_on_amd_weights.bin.
5. Return to AMD-DevTest.exe and run the test. File presence is not readiness.

The test temporarily writes its public INI settings and restores the prior
bytes afterwards. A .before-devtest backup is available after interruption.
Do not run any other app using this folder during the test.

Do NOT copy version.dll into the main DLSS5Tool or Windows directory.
Do NOT upload this folder. Send only results/.../feedback-*.zip.

This experimental harness is NOT an official AMD or NVIDIA product. A positive
test only observes an output difference; it does not certify visual parity,
correct temporal history, HDR support, or readiness for production use.
