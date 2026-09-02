
import os

TEMP_DIR = "/app/app/temp"  

def cleanup():
    if not os.path.isdir(TEMP_DIR):
        print(f"Directory not found: {TEMP_DIR}")
        return

    deleted = 0
    failed = 0

    for filename in os.listdir(TEMP_DIR):
        file_path = os.path.join(TEMP_DIR, filename)
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
                deleted += 1
            except Exception as e:
                print(f"Failed to delete {file_path}: {e}")
                failed += 1

    print(f"Done. Deleted: {deleted}, Failed: {failed}")

if __name__ == "__main__":
    cleanup()