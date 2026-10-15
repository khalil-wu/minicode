import type { AdditionalPermissionProfile } from "../protocol/common-types";

type FileSystemDetail = { kind: "fileSystem"; access: "read" | "write" | "readWrite"; paths: string[] };
export type PermissionRequestDetail = { kind: "network" } | FileSystemDetail;

// Installed desktop permission-request-model-5651a96c7a72: combine read/write
// for the same path and keep explicit special paths in their Source spelling.
export const permissionRequestDetails = (permissions: AdditionalPermissionProfile): PermissionRequestDetail[] => {
  const details: PermissionRequestDetail[] = [];
  if (permissions.network != null) details.push({ kind: "network" });
  const fs = permissions.file_system;
  if (!fs) return details;
  const read = new Set(fs.read ?? []), write = new Set(fs.write ?? []);
  for (const entry of fs.entries ?? []) {
    let path: string;
    switch (entry.path.type) {
      case "path": path = entry.path.path!; break;
      case "glob_pattern": path = entry.path.pattern!; break;
      case "special": {
        const special = entry.path.value!;
        switch (special.kind) {
          case "root": path = "/"; break;
          case "minimal": path = ":minimal"; break;
          case "project_roots": path = special.subpath == null ? ":project_roots" : `:project_roots/${special.subpath}`; break;
          case "tmpdir": path = ":tmpdir"; break;
          case "slash_tmp": path = "/tmp"; break;
          case "unknown": path = special.subpath == null ? special.path! : `${special.path}/${special.subpath}`; break;
        }
        break;
      }
    }
    if (entry.access === "read") read.add(path);
    if (entry.access === "write") write.add(path);
  }
  const both = [...read].filter(path => write.has(path));
  const readOnly = [...read].filter(path => !write.has(path));
  const writeOnly = [...write].filter(path => !read.has(path));
  if (both.length) details.push({ kind: "fileSystem", access: "readWrite", paths: both });
  if (readOnly.length) details.push({ kind: "fileSystem", access: "read", paths: readOnly });
  if (writeOnly.length) details.push({ kind: "fileSystem", access: "write", paths: writeOnly });
  return details;
};
