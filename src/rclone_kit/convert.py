from rclone_kit.dir import Dir
from rclone_kit.file import File
from rclone_kit.remote import Remote


def convert_to_filestr_list(files: str | File | list[str] | list[File]) -> list[str]:
    out: list[str] = []
    if isinstance(files, str):
        out.append(files)
    elif isinstance(files, File):
        out.append(str(files.path))
    elif isinstance(files, list):
        for f in files:
            file_str = str(f.path) if isinstance(f, File) else f
            out.append(file_str)
    else:
        raise ValueError(f"Invalid type for file: {type(files)}")
    return out


def convert_to_str(file_or_dir: str | File | Dir | Remote) -> str:
    if isinstance(file_or_dir, str):
        return file_or_dir
    if isinstance(file_or_dir, File):
        return str(file_or_dir.path)
    if isinstance(file_or_dir, Dir):
        return str(file_or_dir.path)
    if isinstance(file_or_dir, Remote):
        return str(file_or_dir)
    raise ValueError(f"Invalid type for file_or_dir: {type(file_or_dir)}")
