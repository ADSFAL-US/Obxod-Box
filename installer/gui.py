"""
GUI Installer for Obxod-BOX using tkinter.
"""
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from installer.core import InstallConfig, InstallerError, InstallMode, ObxodBoxInstaller


class InstallerGUI:
    """GUI Installer for Obxod-BOX."""
    
    def __init__(self, payload_path: Path, install_dir: Optional[str] = None,
                 no_shortcuts: bool = False, restart: bool = False,
                 runtime_source: Optional[str] = None):
        self.payload_path = Path(payload_path)
        self.installer: Optional[ObxodBoxInstaller] = None
        self.install_thread: Optional[threading.Thread] = None
        self.config: Optional[InstallConfig] = None
        self.no_shortcuts = no_shortcuts
        self.restart = restart
        self.runtime_source = Path(runtime_source) if runtime_source else None

        # Default install path
        self.default_install_path = Path(install_dir) if install_dir else Path("C:/obxod-box")
        
        self.root = tk.Tk()
        self.root.title("Obxod-BOX Setup")
        self.root.geometry("550x550")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # Center window
        self.center_window()
        
        # Variables
        self.install_path_var = tk.StringVar(value=str(self.default_install_path))
        self.create_desktop_shortcut_var = tk.BooleanVar(value=not no_shortcuts)
        self.create_start_menu_shortcut_var = tk.BooleanVar(value=not no_shortcuts)
        self.run_after_install_var = tk.BooleanVar(value=restart)
        
        self.setup_ui()
        
    def center_window(self):
        """Center the window on screen."""
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() // 2) - (width // 2)
        y = (self.root.winfo_screenheight() // 2) - (height // 2)
        self.root.geometry(f'{width}x{height}+{x}+{y}')
    
    def setup_ui(self):
        """Setup the UI components."""
        # Main frame
        main_frame = ttk.Frame(self.root, padding="20")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Title
        title_label = ttk.Label(
            main_frame, 
            text="Obxod-BOX Setup", 
            font=("Segoe UI", 16, "bold")
        )
        title_label.pack(pady=(0, 5))
        
        # Subtitle
        subtitle_label = ttk.Label(
            main_frame,
            text="VPN Client Installation Wizard",
            font=("Segoe UI", 9),
            foreground="gray"
        )
        subtitle_label.pack(pady=(0, 20))
        
        # Separator
        ttk.Separator(main_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(0, 20))
        
        # Install path section
        path_frame = ttk.LabelFrame(main_frame, text="Installation Location", padding="10")
        path_frame.pack(fill=tk.X, pady=(0, 15))
        
        path_entry_frame = ttk.Frame(path_frame)
        path_entry_frame.pack(fill=tk.X)
        
        self.path_entry = ttk.Entry(
            path_entry_frame, 
            textvariable=self.install_path_var,
            font=("Segoe UI", 9)
        )
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        
        browse_btn = ttk.Button(
            path_entry_frame,
            text="Browse...",
            command=self.browse_path
        )
        browse_btn.pack(side=tk.RIGHT)
        
        # Options section
        options_frame = ttk.LabelFrame(main_frame, text="Options", padding="10")
        options_frame.pack(fill=tk.X, pady=(0, 15))
        
        ttk.Checkbutton(
            options_frame,
            text="Create desktop shortcut",
            variable=self.create_desktop_shortcut_var
        ).pack(anchor=tk.W, pady=2)
        
        ttk.Checkbutton(
            options_frame,
            text="Create Start Menu shortcut",
            variable=self.create_start_menu_shortcut_var
        ).pack(anchor=tk.W, pady=2)
        
        ttk.Checkbutton(
            options_frame,
            text="Launch Obxod-BOX after installation",
            variable=self.run_after_install_var
        ).pack(anchor=tk.W, pady=2)
        
        # Progress section
        progress_frame = ttk.LabelFrame(main_frame, text="Progress", padding="10")
        progress_frame.pack(fill=tk.X, pady=(0, 15))
        
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            progress_frame,
            variable=self.progress_var,
            maximum=100,
            length=450
        )
        self.progress_bar.pack(fill=tk.X, pady=(0, 5))
        
        self.status_var = tk.StringVar(value="Ready to install")
        self.status_label = ttk.Label(
            progress_frame,
            textvariable=self.status_var,
            font=("Segoe UI", 9)
        )
        self.status_label.pack(anchor=tk.W)
        
        # Buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=(10, 0))
        
        self.cancel_btn = ttk.Button(
            button_frame,
            text="Cancel",
            command=self.on_close,
            state=tk.NORMAL
        )
        self.cancel_btn.pack(side=tk.RIGHT, padx=(10, 0))
        
        self.install_btn = ttk.Button(
            button_frame,
            text="Install",
            command=self.start_installation,
            style="Accent.TButton"
        )
        self.install_btn.pack(side=tk.RIGHT)
        
        # Style for accent button
        style = ttk.Style()
        style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"))
    
    def browse_path(self):
        """Open folder browser dialog."""
        path = filedialog.askdirectory(
            title="Select Installation Directory",
            initialdir=str(self.default_install_path.parent)
        )
        if path:
            self.install_path_var.set(path)
    
    def update_progress(self, percent: int, message: str):
        """Update progress bar and status (called from installer thread)."""
        self.root.after(0, lambda: self._update_progress_ui(percent, message))
    
    def _update_progress_ui(self, percent: int, message: str):
        """Update UI elements (must be called from main thread)."""
        self.progress_var.set(percent)
        self.status_var.set(message)
        self.root.update_idletasks()
    
    def installation_complete(self, success: bool, error_msg: str = ""):
        """Called when installation completes."""
        self.root.after(0, lambda: self._installation_complete_ui(success, error_msg))
    
    def _installation_complete_ui(self, success: bool, error_msg: str = ""):
        """Handle installation completion in UI thread."""
        self.cancel_btn.config(state=tk.NORMAL, text="Close")
        self.path_entry.config(state=tk.NORMAL)

        if success:
            # Установка завершена — повторять нечего, оставляем только «Закрыть».
            self.install_btn.config(state=tk.DISABLED, text="Install")
            self.progress_var.set(100)
            self.status_var.set("Installation completed successfully!")
            messagebox.showinfo(
                "Success",
                f"Obxod-BOX has been installed to:\n{self.config.install_path}\n\n"
                "You can now launch it from the desktop or Start Menu."
            )
        else:
            # Ошибка — даём повторить попытку.
            self.install_btn.config(state=tk.NORMAL, text="Retry")
            self.status_var.set("Installation failed")
            messagebox.showerror(
                "Installation Failed",
                f"Failed to install Obxod-BOX:\n\n{error_msg}"
            )
    
    def start_installation(self):
        """Start the installation process in a background thread."""
        # Validate path
        install_path = Path(self.install_path_var.get()).resolve()
        if not install_path.parent.exists():
            messagebox.showerror("Error", "Parent directory does not exist!")
            return
        
        # Disable UI during installation
        self.install_btn.config(state=tk.DISABLED, text="Installing...")
        self.cancel_btn.config(state=tk.DISABLED)
        self.path_entry.config(state=tk.DISABLED)
        
        # Create config
        self.config = InstallConfig(
            install_path=install_path,
            create_shortcut=not self.no_shortcuts,
            create_desktop_shortcut=self.create_desktop_shortcut_var.get(),
            create_start_menu_shortcut=self.create_start_menu_shortcut_var.get(),
            run_after_install=self.run_after_install_var.get(),
            mode=InstallMode.GUI,
            runtime_source=self.runtime_source,
        )
        
        # Create installer
        self.installer = ObxodBoxInstaller(self.config, self.update_progress)
        
        # Start installation thread
        self.install_thread = threading.Thread(target=self.run_installation, daemon=True)
        self.install_thread.start()
    
    def run_installation(self):
        """Run installation in background thread."""
        try:
            # Используем путь, который передан из install.bat
            success = self.installer.install(self.payload_path)
            self.installation_complete(success)
        except Exception as e:
            self.installation_complete(False, str(e))
    
    def on_close(self):
        """Handle window close."""
        if self.install_thread and self.install_thread.is_alive():
            if messagebox.askyesno("Cancel", "Installation in progress. Are you sure you want to cancel?"):
                self.root.destroy()
        else:
            self.root.destroy()
    
    def run(self):
        """Run the GUI main loop."""
        self.root.mainloop()


def run_gui_installer(payload_path: Path, silent: bool = False,
                      install_dir: Optional[str] = None,
                      no_shortcuts: bool = False,
                      restart: bool = False,
                      runtime_source: Optional[str] = None) -> int:
    """Entry point for GUI installer.

    When silent is True the installation runs headless with the given options
    (used by the SFX AutoInstall path). Returns a process exit code.
    """
    if silent:
        return run_silent_installer(
            payload_path, install_dir, no_shortcuts, restart, runtime_source
        )

    app = InstallerGUI(payload_path, install_dir, no_shortcuts, restart, runtime_source)
    app.run()
    return 0


def run_silent_installer(payload_path: Path, install_dir: Optional[str] = None,
                         no_shortcuts: bool = False, restart: bool = False,
                         runtime_source: Optional[str] = None) -> int:
    """Run installation without any UI. Returns process exit code."""
    config = InstallConfig(
        install_path=Path(install_dir) if install_dir else Path("C:/obxod-box"),
        create_shortcut=not no_shortcuts,
        create_desktop_shortcut=not no_shortcuts,
        create_start_menu_shortcut=not no_shortcuts,
        run_after_install=restart,
        mode=InstallMode.GUI,
        silent=True,
        runtime_source=Path(runtime_source) if runtime_source else None,
    )
    installer = ObxodBoxInstaller(config)
    try:
        return 0 if installer.install(Path(payload_path)) else 1
    except Exception as exc:  # noqa: BLE001 - report any failure as exit code
        print(f"Installation failed: {exc}", file=sys.stderr)
        return 1