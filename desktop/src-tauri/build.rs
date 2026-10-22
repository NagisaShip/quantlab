fn main() {
    // 为应用命令生成 ACL 权限（allow-native-*），远程来源（本机后端页面）通过 capability 引用这些权限。
    let attributes = tauri_build::Attributes::default().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "native_choose_folder",
            "native_choose_files",
            "native_desktop_info",
            "native_open_project",
            "native_reveal_path",
            "native_open_path",
        ]),
    );
    tauri_build::try_build(attributes).expect("failed to run tauri-build");
}
