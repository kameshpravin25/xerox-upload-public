/**
 * Xerox Upload System - PDF Preview
 * In-browser PDF viewing with page selection using PDF.js
 */

class PDFPreview {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.pdfDoc = null;
        this.pages = [];
        this.selectedPages = new Set();
        this.rotations = {}; // {pageNum: degrees}
        this.scale = 0.2; // Thumbnail scale (smaller = faster rendering)
        this.onPageCountChange = null;
        this.onSelectionChange = null;
        this._loading = false; // Guard against concurrent loadPDF calls
        this._loadId = 0;      // Monotonic ID to invalidate stale renders

        // PDF.js library reference
        this.pdfjsLib = window.pdfjsLib;
        if (this.pdfjsLib) {
            this.pdfjsLib.GlobalWorkerOptions.workerSrc =
                'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
        }
    }

    /**
     * Load a PDF file and render thumbnails
     */
    async loadPDF(file) {
        if (!this.pdfjsLib) {
            console.error('PDF.js library not loaded');
            return { success: false, error: 'library_missing' };
        }

        // Invalidate any in-progress load/render
        const myLoadId = ++this._loadId;
        this._loading = true;

        try {
            const arrayBuffer = await file.arrayBuffer();

            // If a newer loadPDF was called while we were reading the file, bail out
            if (myLoadId !== this._loadId) {
                return { success: false, error: 'superseded' };
            }

            this.pdfDoc = await this.pdfjsLib.getDocument({ data: arrayBuffer }).promise;

            // Check again after async document parsing
            if (myLoadId !== this._loadId) {
                return { success: false, error: 'superseded' };
            }

            // Force PDF.js to fully parse the document structure.
            // Some scanned/image-heavy PDFs have incremental cross-reference tables
            // that cause numPages to be under-reported until all pages are accessed.
            let pageCount = this.pdfDoc.numPages;
            try {
                await this.pdfDoc.getPage(pageCount);
                // Re-read in case parsing the last page updated internal state
                pageCount = this.pdfDoc.numPages;
            } catch (e) {
                // Page access failed — use the count we have
            }

            this.pages = [];
            this.selectedPages = new Set();
            this.rotations = {};

            // Select all pages by default
            for (let i = 1; i <= pageCount; i++) {
                this.selectedPages.add(i);
            }

            // Notify page count change
            if (this.onPageCountChange) {
                this.onPageCountChange(pageCount);
            }

            // Render thumbnails (passes loadId so stale renders stop)
            await this.renderThumbnails(myLoadId);

            // Post-render verification: some PDFs update numPages during page rendering.
            // If the count changed, re-render with the correct page count.
            const finalPageCount = this.pdfDoc.numPages;
            if (finalPageCount !== pageCount && myLoadId === this._loadId) {
                console.info(`PDF page count corrected: ${pageCount} → ${finalPageCount}`);
                pageCount = finalPageCount;
                this.pages = [];
                this.selectedPages = new Set();
                for (let i = 1; i <= pageCount; i++) {
                    this.selectedPages.add(i);
                }
                if (this.onPageCountChange) {
                    this.onPageCountChange(pageCount);
                }
                await this.renderThumbnails(myLoadId);
            }

            // Final check — if superseded during rendering, don't report success
            if (myLoadId !== this._loadId) {
                return { success: false, error: 'superseded' };
            }

            this._loading = false;
            return { success: true };
        } catch (error) {
            this._loading = false;
            console.error('Error loading PDF:', error);

            // Detect password-protected / encrypted PDFs
            if (error.name === 'PasswordException' || 
                (error.message && error.message.toLowerCase().includes('password'))) {
                return { success: false, error: 'password_protected' };
            }

            return { success: false, error: 'load_failed' };
        }
    }

    /**
     * Render all page thumbnails progressively.
     * Pages rendered in parallel batches of 4 for speed.
     */
    async renderThumbnails(loadId) {
        if (!this.pdfDoc || !this.container) return;

        this.container.innerHTML = '';
        this.container.className = 'pdf-thumbnail-grid';
        const totalPages = this.pdfDoc.numPages;
        const BATCH = 4;

        // Show skeleton placeholders immediately so the grid isn't empty
        const skeletons = [];
        for (let i = 1; i <= totalPages; i++) {
            const skeleton = document.createElement('div');
            skeleton.className = 'pdf-thumbnail-skeleton';
            skeleton.dataset.page = i;
            this.container.appendChild(skeleton);
            skeletons.push(skeleton);
        }

        for (let i = 1; i <= totalPages; i += BATCH) {
            // Stop rendering if a newer load has started
            if (loadId !== undefined && loadId !== this._loadId) return;

            const batch = [];
            for (let j = i; j < i + BATCH && j <= totalPages; j++) {
                batch.push(j);
            }

            // Render batch in parallel
            const results = await Promise.all(batch.map(async (pageNum) => {
                try {
                    const page = await this.pdfDoc.getPage(pageNum);
                    return { pageNum, thumbnail: await this.createThumbnail(page, pageNum) };
                } catch (err) {
                    console.warn(`Failed to render page ${pageNum}:`, err);
                    return null;
                }
            }));

            // Stop appending if superseded during batch render
            if (loadId !== undefined && loadId !== this._loadId) return;

            // Replace skeletons with real thumbnails
            for (const result of results) {
                if (result) {
                    result.thumbnail.classList.add('pdf-thumbnail-loaded');
                    const skeleton = skeletons[result.pageNum - 1];
                    if (skeleton && skeleton.parentNode) {
                        skeleton.replaceWith(result.thumbnail);
                    } else {
                        this.container.appendChild(result.thumbnail);
                    }
                    this.pages.push({ pageNum: result.pageNum, element: result.thumbnail });
                }
            }

            // Yield to browser between batches
            await new Promise(r => requestAnimationFrame(r));
        }
    }

    /**
     * Create a fully rendered thumbnail (used by renderThumbnails and rotatePage).
     */
    async createThumbnail(page, pageNum) {
        const viewport = page.getViewport({ scale: this.scale, rotation: this.rotations[pageNum] || 0 });

        const wrapper = document.createElement('div');
        wrapper.className = 'pdf-thumbnail pdf-thumbnail-loaded';
        wrapper.dataset.page = pageNum;

        if (this.selectedPages.has(pageNum)) {
            wrapper.classList.add('selected');
        }

        // Canvas for rendering
        const canvas = document.createElement('canvas');
        canvas.width = viewport.width;
        canvas.height = viewport.height;

        const ctx = canvas.getContext('2d');
        await page.render({ canvasContext: ctx, viewport }).promise;

        // Page number label
        const label = document.createElement('div');
        label.className = 'pdf-page-label';
        label.textContent = pageNum;

        // Custom checkmark indicator (replaces native checkbox)
        const checkIndicator = document.createElement('div');
        checkIndicator.className = 'pdf-check-indicator';
        checkIndicator.innerHTML = `<svg viewBox="0 0 24 24" fill="none"><path d="M5 13l4 4L19 7" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

        // Rotation button
        const rotateBtn = document.createElement('button');
        rotateBtn.type = 'button';
        rotateBtn.className = 'pdf-rotate-btn';
        rotateBtn.innerHTML = '↻';
        rotateBtn.title = 'Rotate 90°';
        rotateBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this.rotatePage(pageNum);
        });

        wrapper.appendChild(canvas);
        wrapper.appendChild(checkIndicator);
        wrapper.appendChild(label);
        wrapper.appendChild(rotateBtn);

        // Click anywhere on thumbnail to toggle selection
        wrapper.addEventListener('click', () => {
            this.togglePage(pageNum);
        });

        return wrapper;
    }

    /**
     * Toggle page selection (enforces min 1 page)
     */
    togglePage(pageNum) {
        if (this.selectedPages.has(pageNum)) {
            // Don't allow deselecting if it's the last selected page
            if (this.selectedPages.size <= 1) {
                return; // Keep at least 1 page selected
            }
            this.selectedPages.delete(pageNum);
        } else {
            this.selectedPages.add(pageNum);
        }

        // Update visual state
        const element = this.pages.find(p => p.pageNum === pageNum)?.element;
        if (element) {
            element.classList.toggle('selected', this.selectedPages.has(pageNum));
        }

        if (this.onSelectionChange) {
            this.onSelectionChange(this.getSelectedPages());
        }
    }

    /**
     * Rotate a page by 90 degrees
     */
    async rotatePage(pageNum) {
        const currentRotation = this.rotations[pageNum] || 0;
        this.rotations[pageNum] = (currentRotation + 90) % 360;

        // Re-render the thumbnail
        const pageData = this.pages.find(p => p.pageNum === pageNum);
        if (pageData && this.pdfDoc) {
            const page = await this.pdfDoc.getPage(pageNum);
            const newThumbnail = await this.createThumbnail(page, pageNum);
            pageData.element.replaceWith(newThumbnail);
            pageData.element = newThumbnail;
        }
    }

    /**
     * Select all pages
     */
    selectAll() {
        for (let i = 1; i <= this.pdfDoc?.numPages || 0; i++) {
            this.selectedPages.add(i);
        }
        this.updateAllVisuals();
        if (this.onSelectionChange) {
            this.onSelectionChange(this.getSelectedPages());
        }
    }

    /**
     * Deselect all except page 1 (minimum 1 page required)
     */
    deselectAll() {
        this.selectedPages.clear();
        this.selectedPages.add(1); // Keep at least page 1
        this.updateAllVisuals();
        if (this.onSelectionChange) {
            this.onSelectionChange(this.getSelectedPages());
        }
    }

    /**
     * Set selection from page range string
     */
    setPageRange(rangeStr) {
        if (!this.pdfDoc) return;

        const maxPages = this.pdfDoc.numPages;
        const pages = parsePageRange(rangeStr, maxPages);

        if (pages.length === 0) {
            // "all" or empty = select all
            this.selectAll();
        } else {
            this.selectedPages = new Set(pages);
            // Ensure at least 1 page is selected
            if (this.selectedPages.size === 0) {
                this.selectedPages.add(1);
            }
            this.updateAllVisuals();
            if (this.onSelectionChange) {
                this.onSelectionChange(this.getSelectedPages());
            }
        }
    }

    /**
     * Update all visual states
     */
    updateAllVisuals() {
        this.pages.forEach(({ pageNum, element }) => {
            const isSelected = this.selectedPages.has(pageNum);
            element.classList.toggle('selected', isSelected);
        });
    }

    /**
     * Get sorted array of selected page numbers
     */
    getSelectedPages() {
        return Array.from(this.selectedPages).sort((a, b) => a - b);
    }

    /**
     * Get rotations object
     */
    getRotations() {
        return { ...this.rotations };
    }

    /**
     * Get total page count
     */
    getPageCount() {
        return this.pdfDoc?.numPages || 0;
    }

    /**
     * Get job metadata for upload
     */
    getJobMetadata() {
        return {
            page_list: this.getSelectedPages(),
            rotations: this.getRotations(),
            total_pages: this.getPageCount(),
            selected_count: this.selectedPages.size
        };
    }

    /**
     * Clear the preview
     */
    clear() {
        this.pdfDoc = null;
        this.pages = [];
        this.selectedPages = new Set();
        this.rotations = {};
        if (this.container) {
            // Remove progress bar if still present
            const progress = this.container.parentNode?.querySelector('.pdf-render-progress');
            if (progress) progress.remove();
            this.container.innerHTML = '';
        }
    }
}

// Export for use in other modules
if (typeof window !== 'undefined') {
    window.PDFPreview = PDFPreview;
}
