fn main() {
    // generate_context! embeds cached icon bytes. Track the source files so
    // replacing an icon also rebuilds the native executable used by tauri dev.
    for icon in ["icons/icon.png", "icons/icon.icns", "icons/icon.ico"] {
        println!("cargo:rerun-if-changed={icon}");
    }
    tauri_build::build()
}
