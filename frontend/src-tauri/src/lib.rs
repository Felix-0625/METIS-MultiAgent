#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .invoke_handler(tauri::generate_handler![save_project_archive])
    .setup(|app| {
      if cfg!(debug_assertions) {
        app.handle().plugin(
          tauri_plugin_log::Builder::default()
            .level(log::LevelFilter::Info)
            .build(),
        )?;
      }
      Ok(())
    })
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
}

#[tauri::command]
fn save_project_archive(directory: String, filename: String, bytes: Vec<u8>) -> Result<String, String> {
  let dir = std::path::PathBuf::from(directory);
  if dir.as_os_str().is_empty() {
    return Err("Archive directory is empty".to_string());
  }
  if filename.contains("..") || filename.contains('/') || filename.contains('\\') {
    return Err("Invalid archive filename".to_string());
  }

  std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
  let target = dir.join(filename);
  std::fs::write(&target, bytes).map_err(|e| e.to_string())?;
  Ok(target.to_string_lossy().to_string())
}
