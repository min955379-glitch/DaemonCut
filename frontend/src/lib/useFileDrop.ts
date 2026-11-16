import { useCallback, useRef, useState } from "react";

/** Video suffixes accepted by the clipper ingest path (matches backend _VIDEO_SUFFIXES). */
export const DROPPABLE_VIDEO_SUFFIXES = [".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".ts"];

const MAX_DROP_FILE_BYTES = 2 * 1024 * 1024 * 1024; // keep parity with the backend size guard

export function looksLikeVideoFile(name: string): boolean {
  const suffix = name.slice(name.lastIndexOf(".")).toLowerCase();
  return DROPPABLE_VIDEO_SUFFIXES.includes(suffix);
}

interface UseFileDropOptions {
  /** Called with the dropped file's local path and a suggested title. */
  onDrop: (path: string, suggestedTitle: string) => void;
  /** Optional toast callback for validation feedback. */
  onToast?: (message: string, kind?: "info" | "alert" | "success") => void;
}

/**
 * Drag-and-drop helper for media-file path fields.
 *
 * Drops a file onto the returned drop handlers and resolves to the file's
 * local path string, which the backend ingests directly (it copies files
 * outside the Drive root into source_media/vods automatically).
 */
export function useFileDrop({ onDrop, onToast }: UseFileDropOptions) {
  const [isDragging, setIsDragging] = useState(false);
  const dragDepth = useRef(0);

  const handleDragEnter = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.stopPropagation();
    dragDepth.current += 1;
    setIsDragging(true);
  }, []);

  const handleDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.stopPropagation();
  }, []);

  const handleDragLeave = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.stopPropagation();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) {
      setIsDragging(false);
    }
  }, []);

  const handleDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      event.stopPropagation();
      dragDepth.current = 0;
      setIsDragging(false);
      const file = event.dataTransfer?.files?.[0];
      if (!file) {
        onToast?.("Nothing was dropped. Drag a video file onto the field.", "alert");
        return;
      }
      // Chromium exposes the local file path via a non-standard `path` property.
      const localPath = (file as File & { path?: string }).path;
      const path = typeof localPath === "string" && localPath.trim() ? localPath : null;
      if (!path) {
        onToast?.("Could not read the dropped file's path. Type the local path manually.", "alert");
        return;
      }
      if (!looksLikeVideoFile(file.name)) {
        const allowed = DROPPABLE_VIDEO_SUFFIXES.join(", ");
        onToast?.(`Unsupported file type: ${file.name}. Use one of: ${allowed}.`, "alert");
        return;
      }
      if (file.size > MAX_DROP_FILE_BYTES) {
        onToast?.("Dropped file exceeds the 2 GB media limit.", "alert");
        return;
      }
      const suggestedTitle = file.name.replace(/\.[^.]+$/, "");
      onDrop(path, suggestedTitle);
      onToast?.(`Dropped ${file.name} — path filled in.`, "success");
    },
    [onDrop, onToast],
  );

  return { isDragging, dragHandlers: { onDragEnter: handleDragEnter, onDragOver: handleDragOver, onDragLeave: handleDragLeave, onDrop: handleDrop } };
}
