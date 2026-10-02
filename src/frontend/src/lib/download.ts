/** Hand a file the page already holds to the browser's download manager. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.hidden = true;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Safari only starts reading the URL after the click has returned; give it time before letting go.
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
