import { useEffect, useRef, type ReactNode } from "react";

/**
 * A modal on the native `<dialog>`, which gives focus trapping, Escape and the inert
 * background for free -- the three things a hand-built overlay gets wrong first.
 */
export function Dialog({
  open,
  onClose,
  title,
  children,
  actions,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  actions: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (el === null) return;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);

  return (
    <dialog ref={ref} className="dlg" onClose={onClose} aria-labelledby="dlg-title">
      {open && (
        <>
          <div className="dlg__body">
            <h2 className="dlg__title" id="dlg-title">
              {title}
            </h2>
            {children}
          </div>
          <div className="cft" style={{ justifyContent: "flex-end" }}>
            {actions}
          </div>
        </>
      )}
    </dialog>
  );
}
