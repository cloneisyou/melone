; Kill the bundled Python daemon (a separate process from Melone.exe) before
; install and uninstall. electron-builder's default NSIS template only stops
; Melone.exe, so melone-daemon.exe would otherwise keep the SQLite DB — and its
; own .exe — file-locked, breaking the update/reinstall. /T also kills children.
!macro customInstall
  nsExec::Exec 'taskkill /F /T /IM melone-daemon.exe'
!macroend

!macro customUnInstall
  nsExec::Exec 'taskkill /F /T /IM melone-daemon.exe'
!macroend
