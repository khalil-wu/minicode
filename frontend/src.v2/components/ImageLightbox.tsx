import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { useFocusTrap } from "../hooks/useFocusTrap";
import "./image-lightbox.css";

interface ImageLightboxProps {
  src: string;
  alt: string;
  title?: string;
  onClose: () => void;
}

export function ImageLightbox({ src, alt, title, onClose }: ImageLightboxProps) {
  const dialogRef = useFocusTrap(true);

  const content = (
    <div className="image-lightbox-backdrop" onClick={onClose}>
      <div
        ref={dialogRef}
        className="image-lightbox-panel"
        role="dialog"
        aria-modal="true"
        aria-label={title ? `Preview ${title}` : "Preview image"}
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === "Escape") onClose();
        }}
      >
        <button
          type="button"
          className="image-lightbox-close"
          aria-label="Close image preview"
          title="Close"
          onClick={onClose}
        >
          <X size={18} />
        </button>
        <img className="image-lightbox-img" src={src} alt={alt} />
        {title ? <div className="image-lightbox-caption">{title}</div> : null}
      </div>
    </div>
  );

  return createPortal(content, document.body);
}
