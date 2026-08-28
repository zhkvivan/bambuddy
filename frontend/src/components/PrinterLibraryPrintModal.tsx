import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Minus, Plus, Search, X } from 'lucide-react';
import { api, type LibraryFileListItem } from '../api/client';
import { Button } from './Button';

type Item = { key: string; quantity: number };

interface Props {
  printerName: string;
  onClose: () => void;
  onMerged: (file: { id: number; filename: string }) => void;
}

function projectFiles(files: LibraryFileListItem[] | undefined): LibraryFileListItem[] {
  return (files ?? []).filter((file) => {
    const name = file.filename.toLowerCase();
    return name.endsWith('.3mf') && !name.endsWith('.gcode.3mf');
  });
}

function stem(filename: string): string {
  return filename.replace(/\.3mf$/i, '').trim().toUpperCase();
}

/** The printer-first quick order flow. It deliberately produces a normal
 * temporary 3MF and hands it to SliceModal, so arranging and slicing still
 * have exactly one implementation. */
export function PrinterLibraryPrintModal({ printerName, onClose, onMerged }: Props) {
  const { data: library, isLoading } = useQuery({
    queryKey: ['library-files', 'printer-quick-print'],
    // `includeRoot=false` with no folder is the library API's explicit
    // "all folders" mode. Letter collections are normally kept in a folder,
    // so limiting this printer-first workflow to root makes valid A.3mf etc.
    // look as though they don't exist.
    queryFn: () => api.getLibraryFiles(undefined, false),
  });
  const [mode, setMode] = useState<'letters' | 'files'>('letters');
  const [rows, setRows] = useState<Item[]>([{ key: '', quantity: 1 }]);
  const [filter, setFilter] = useState('');
  const inputRefs = useRef<Array<HTMLInputElement | null>>([]);
  const [focusRow, setFocusRow] = useState(0);

  // Typing a word should feel like a tiny order form, not like filling out a
  // dialog: focus the first line on open, and move it to each row added with
  // Enter (or the add-row button).
  useEffect(() => {
    if (mode !== 'letters') return;
    inputRefs.current[focusRow]?.focus();
  }, [focusRow, mode, rows.length]);

  const files = useMemo(() => projectFiles(library), [library]);
  const filtered = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return needle ? files.filter((file) => file.filename.toLowerCase().includes(needle)) : files;
  }, [files, filter]);

  const merge = useMutation({
    mutationFn: async () => {
      const byStem = new Map(files.map((file) => [stem(file.filename), file]));
      const wanted = rows.filter((row) => row.key.trim() && row.quantity > 0);
      const missing = wanted.filter((row) => !byStem.has(row.key.trim().toUpperCase()));
      if (missing.length) {
        throw new Error(`В библиотеке не найдены: ${missing.map((row) => row.key.trim().toUpperCase()).join(', ')}`);
      }
      if (!wanted.length) throw new Error('Добавь хотя бы одну букву или модель');
      const ids = wanted.flatMap((row) => {
        const file = byStem.get(row.key.trim().toUpperCase())!;
        return Array.from({ length: Math.min(50, row.quantity) }, () => file.id);
      });
      if (ids.length > 50) throw new Error('На одну пластину можно собрать не больше 50 объектов');
      const label = wanted.map((row) => `${row.key.trim().toUpperCase()}${row.quantity > 1 ? `x${row.quantity}` : ''}`).join('-');
      return api.mergeLibraryFilesOnPlate(ids, `${label}.3mf`);
    },
    onSuccess: (file) => onMerged(file),
  });

  const update = (index: number, patch: Partial<Item>) => {
    setRows((current) => current.map((row, i) => i === index ? { ...row, ...patch } : row));
  };
  const addRow = () => {
    setRows((current) => [...current, { key: '', quantity: 1 }]);
    setFocusRow(rows.length);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onMouseDown={onClose}>
      <div className="flex max-h-[85vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary" onMouseDown={(event) => event.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-bambu-dark-tertiary px-6 py-4">
          <div>
            <h2 className="text-xl font-semibold text-white">Печать на {printerName}</h2>
            <p className="mt-1 text-sm text-bambu-gray">Соберём выбранные 3MF на одну пластину, затем откроется нарезка.</p>
          </div>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Закрыть"><X className="h-5 w-5" /></Button>
        </div>

        <div className="flex gap-2 border-b border-bambu-dark-tertiary px-6 pt-4">
          <button className={`border-b-2 px-3 pb-3 text-sm font-medium ${mode === 'letters' ? 'border-bambu-green text-bambu-green' : 'border-transparent text-bambu-gray'}`} onClick={() => setMode('letters')}>Буквы / слово</button>
          <button className={`border-b-2 px-3 pb-3 text-sm font-medium ${mode === 'files' ? 'border-bambu-green text-bambu-green' : 'border-transparent text-bambu-gray'}`} onClick={() => setMode('files')}>Модели из библиотеки</button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          {mode === 'letters' ? (
            <>
              <p className="mb-4 text-sm text-bambu-gray">Вводи имя файла без расширения: например <span className="text-white">C</span>, <span className="text-white">O × 2</span>, <span className="text-white">B × 4</span>. Файлы должны называться <span className="text-white">C.3mf</span>, <span className="text-white">O.3mf</span> и т.д.</p>
              <div className="space-y-2">
                {rows.map((row, index) => (
                  <div className="flex items-center gap-2" key={index}>
                    <input ref={(element) => { inputRefs.current[index] = element; }} value={row.key} maxLength={40} onChange={(event) => update(index, { key: event.target.value })} onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); addRow(); } }} placeholder="Буква или имя модели" className="min-w-0 flex-1 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark px-3 py-2 text-white outline-none focus:border-bambu-green" />
                    <Button variant="secondary" size="sm" onClick={() => update(index, { quantity: Math.max(1, row.quantity - 1) })}><Minus className="h-4 w-4" /></Button>
                    <span className="w-7 text-center text-white">{row.quantity}</span>
                    <Button variant="secondary" size="sm" onClick={() => update(index, { quantity: Math.min(50, row.quantity + 1) })}><Plus className="h-4 w-4" /></Button>
                    <Button variant="ghost" size="sm" disabled={rows.length === 1} onClick={() => setRows((current) => current.filter((_, i) => i !== index))}><X className="h-4 w-4" /></Button>
                  </div>
                ))}
              </div>
              <Button variant="secondary" size="sm" className="mt-4" onClick={addRow}><Plus className="mr-1 h-4 w-4" />Добавить строку</Button>
            </>
          ) : (
            <>
              <label className="relative mb-3 block"><Search className="absolute left-3 top-2.5 h-4 w-4 text-bambu-gray" /><input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Найти модель" className="w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark py-2 pl-9 pr-3 text-white outline-none focus:border-bambu-green" /></label>
              <div className="space-y-2">
                {filtered.map((file) => {
                  const index = rows.findIndex((row) => row.key === stem(file.filename));
                  const row = index >= 0 ? rows[index] : null;
                  return <div key={file.id} className="flex items-center gap-3 rounded-lg border border-bambu-dark-tertiary p-3"><button className={`h-5 w-5 rounded border ${row ? 'border-bambu-green bg-bambu-green' : 'border-bambu-gray'}`} onClick={() => setRows((current) => row ? current.filter((_, i) => i !== index) : [...current, { key: stem(file.filename), quantity: 1 }])} aria-label={`Выбрать ${file.filename}`} /><span className="min-w-0 flex-1 truncate text-sm text-white">{file.filename}</span>{row && <><Button variant="secondary" size="sm" onClick={() => update(index, { quantity: Math.max(1, row.quantity - 1) })}><Minus className="h-4 w-4" /></Button><span className="w-5 text-center text-white">{row.quantity}</span><Button variant="secondary" size="sm" onClick={() => update(index, { quantity: Math.min(50, row.quantity + 1) })}><Plus className="h-4 w-4" /></Button></>}</div>;
                })}
                {!isLoading && filtered.length === 0 && <p className="text-sm text-bambu-gray">3MF-моделей не найдено.</p>}
              </div>
            </>
          )}
          {merge.error && <p className="mt-4 text-sm text-red-400">{merge.error.message}</p>}
        </div>
        <div className="flex justify-end gap-3 border-t border-bambu-dark-tertiary px-6 py-4"><Button variant="secondary" onClick={onClose}>Отмена</Button><Button onClick={() => merge.mutate()} disabled={merge.isPending || isLoading}>{merge.isPending ? 'Собираем…' : 'Собрать на одну пластину'}</Button></div>
      </div>
    </div>
  );
}
