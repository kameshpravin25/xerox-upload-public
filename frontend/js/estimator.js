/**
 * Xerox Upload System - Cost Estimator
 * Client-side price calculation based on pricing.json configuration
 */

class CostEstimator {
    constructor() {
        this.pricing = null;
        this.pageCount = 0;
        this.options = {
            colorMode: 'bw',
            paperSize: 'A4',
            duplex: false,
            binding: 'none',
            copies: 1,
            pageRange: 'all',
            selectedPages: []
        };
        this.onEstimateChange = null;
    }

    /**
     * Load pricing configuration from JSON file
     */
    async loadPricing() {
        try {
            const response = await fetch('/pricing.json');
            if (!response.ok) throw new Error('Failed to load pricing');
            this.pricing = await response.json();
            return true;
        } catch (error) {
            console.error('Error loading pricing:', error);
            // Default fallback pricing
            this.pricing = {
                currency: { symbol: '₹', cents_to_display: 100 },
                base_prices: { bw_per_page_cents: 180, bw_duplex_per_page_cents: 200, color_per_page_cents: 500, color_duplex_per_page_cents: 1000 },
                paper_sizes: { A4: { multiplier: 1.0 } },
                duplex: { enabled: true },
                binding: { none: { price_cents: 0 } },
                copies: { bulk_discount: { threshold: 10, discount_percent: 10 } },
                minimum_job_cents: 180
            };
            return false;
        }
    }

    /**
     * Set the total page count (from PDF.js or file detection)
     */
    setPageCount(count) {
        this.pageCount = count;
        this.updateEstimate();
    }

    /**
     * Update an option and recalculate
     */
    setOption(key, value) {
        if (key in this.options) {
            this.options[key] = value;
            this.updateEstimate();
        }
    }

    /**
     * Get the effective page count based on selection
     */
    getEffectivePageCount() {
        if (this.options.pageRange === 'all') {
            return this.pageCount;
        }
        if (this.options.selectedPages && this.options.selectedPages.length > 0) {
            return this.options.selectedPages.length;
        }
        return this.pageCount;
    }

    /**
     * Calculate the total cost in cents
     */
    calculateCostCents() {
        if (!this.pricing) return 0;

        const effectivePages = this.getEffectivePageCount();
        if (effectivePages === 0) return 0;

        // Base price per page (explicit duplex pricing)
        let basePrice;
        if (this.options.colorMode === 'color') {
            basePrice = this.options.duplex
                ? this.pricing.base_prices.color_duplex_per_page_cents
                : this.pricing.base_prices.color_per_page_cents;
        } else {
            basePrice = this.options.duplex
                ? this.pricing.base_prices.bw_duplex_per_page_cents
                : this.pricing.base_prices.bw_per_page_cents;
        }

        // Paper size multiplier
        const paperMultiplier = this.pricing.paper_sizes[this.options.paperSize]?.multiplier || 1.0;

        // Calculate price per unit (per page for single, per sheet for duplex)
        let pricePerUnit = basePrice * paperMultiplier;

        // For duplex, calculate based on sheets (physical papers) needed
        // Each sheet holds 2 pages, so sheets = ceil(pages / 2)
        const billableUnits = this.options.duplex
            ? Math.ceil(effectivePages / 2)
            : effectivePages;

        // Calculate total
        let totalCents = pricePerUnit * billableUnits;

        // Multiply by copies
        totalCents *= this.options.copies;

        // Apply bulk discount if applicable
        const bulkDiscount = this.pricing.copies?.bulk_discount;
        if (bulkDiscount && this.options.copies >= bulkDiscount.threshold) {
            totalCents *= (1 - bulkDiscount.discount_percent / 100);
        }

        // Add binding cost
        const bindingCost = this.pricing.binding[this.options.binding]?.price_cents || 0;
        totalCents += bindingCost * this.options.copies;

        // Apply minimum job charge
        if (totalCents < this.pricing.minimum_job_cents) {
            totalCents = this.pricing.minimum_job_cents;
        }

        return Math.round(totalCents);
    }

    /**
     * Format cents to display currency
     */
    formatPrice(cents) {
        if (!this.pricing) return '—';
        const symbol = this.pricing.currency?.symbol || '₹';
        const divisor = this.pricing.currency?.cents_to_display || 100;
        const amount = (cents / divisor).toFixed(2);
        return `${symbol}${amount}`;
    }

    /**
     * Get detailed price breakdown
     */
    getBreakdown() {
        if (!this.pricing) return null;

        const effectivePages = this.getEffectivePageCount();
        let basePrice;
        if (this.options.colorMode === 'color') {
            basePrice = this.options.duplex
                ? this.pricing.base_prices.color_duplex_per_page_cents
                : this.pricing.base_prices.color_per_page_cents;
        } else {
            basePrice = this.options.duplex
                ? this.pricing.base_prices.bw_duplex_per_page_cents
                : this.pricing.base_prices.bw_per_page_cents;
        }
        const paperMultiplier = this.pricing.paper_sizes[this.options.paperSize]?.multiplier || 1.0;

        let pricePerUnit = basePrice * paperMultiplier;

        // For duplex: bill by sheets (physical papers), not pages
        const isDuplex = this.options.duplex;
        const billableUnits = isDuplex
            ? Math.ceil(effectivePages / 2)
            : effectivePages;

        const unitCost = pricePerUnit * billableUnits;
        const bindingCost = this.pricing.binding[this.options.binding]?.price_cents || 0;

        let subtotal = (unitCost + bindingCost) * this.options.copies;

        const bulkDiscount = this.pricing.copies?.bulk_discount;
        const bulkDiscountApplied = bulkDiscount && this.options.copies >= bulkDiscount.threshold;

        if (bulkDiscountApplied) {
            subtotal *= (1 - bulkDiscount.discount_percent / 100);
        }

        const minimumApplied = subtotal < this.pricing.minimum_job_cents;
        const total = minimumApplied ? this.pricing.minimum_job_cents : Math.round(subtotal);

        return {
            pages: effectivePages,
            sheets: billableUnits,
            isDuplex,
            pricePerUnit: Math.round(pricePerUnit),
            pricePerPage: Math.round(pricePerUnit), // backward compat
            unitCost: Math.round(unitCost),
            pageCost: Math.round(unitCost), // backward compat
            bindingCost,
            copies: this.options.copies,
            duplexDiscount: 0,
            bulkDiscountApplied,
            bulkDiscountPercent: bulkDiscount?.discount_percent || 0,
            minimumApplied,
            minimumCharge: this.pricing.minimum_job_cents,
            total
        };
    }

    /**
     * Trigger estimate update callback
     */
    updateEstimate() {
        if (this.onEstimateChange) {
            const cents = this.calculateCostCents();
            const formatted = this.formatPrice(cents);
            const breakdown = this.getBreakdown();
            this.onEstimateChange(cents, formatted, breakdown);
        }
    }

    /**
     * Get all available options for UI rendering
     */
    getAvailableOptions() {
        if (!this.pricing) return null;

        return {
            paperSizes: Object.entries(this.pricing.paper_sizes).map(([key, val]) => ({
                value: key,
                label: val.label || key,
                multiplier: val.multiplier
            })),
            bindingOptions: Object.entries(this.pricing.binding).map(([key, val]) => ({
                value: key,
                label: val.label || key,
                price: val.price_cents
            })),
            maxCopies: this.pricing.copies?.max || 100,
            bulkThreshold: this.pricing.copies?.bulk_discount?.threshold || 10,
            bulkDiscount: this.pricing.copies?.bulk_discount?.discount_percent || 0,
            duplexEnabled: this.pricing.duplex?.enabled || false,
            currencySymbol: this.pricing.currency?.symbol || '₹'
        };
    }
}

/**
 * Parse page range string to array of page numbers
 * Examples: "1-5" -> [1,2,3,4,5], "1,3,5-7" -> [1,3,5,6,7]
 */
function parsePageRange(rangeStr, maxPages) {
    if (!rangeStr || rangeStr.trim().toLowerCase() === 'all') {
        return [];
    }

    const pages = new Set();
    const parts = rangeStr.split(',');

    for (const part of parts) {
        const trimmed = part.trim();
        if (trimmed.includes('-')) {
            const [start, end] = trimmed.split('-').map(n => parseInt(n.trim(), 10));
            if (!isNaN(start) && !isNaN(end)) {
                for (let i = Math.max(1, start); i <= Math.min(end, maxPages); i++) {
                    pages.add(i);
                }
            }
        } else {
            const num = parseInt(trimmed, 10);
            if (!isNaN(num) && num >= 1 && num <= maxPages) {
                pages.add(num);
            }
        }
    }

    return Array.from(pages).sort((a, b) => a - b);
}

/**
 * Detect page count from file (preview estimate only — server recalculates exact count)
 */
async function detectPageCount(file) {
    const extension = file.name.split('.').pop().toLowerCase();

    // Images are 1 page
    if (['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'tiff'].includes(extension)) {
        return { count: 1, estimated: false };
    }

    // For PDF, we'll use PDF.js (handled separately)
    if (extension === 'pdf') {
        return { count: null, estimated: false, needsPdfJs: true };
    }

    // Word documents — quick size estimate for preview
    // Server will recalculate exact count via LibreOffice after upload
    if (['doc', 'docx', 'odt'].includes(extension)) {
        const estimatedPages = Math.max(1, Math.ceil(file.size / (20 * 1024)));
        return { count: estimatedPages, estimated: true };
    }

    // Plain text formats (~5KB per page)
    if (['rtf', 'txt'].includes(extension)) {
        const estimatedPages = Math.max(1, Math.ceil(file.size / (5 * 1024)));
        return { count: estimatedPages, estimated: true };
    }

    // PowerPoint: estimate ~50KB per slide
    if (['ppt', 'pptx', 'odp'].includes(extension)) {
        const estimatedSlides = Math.max(1, Math.ceil(file.size / (50 * 1024)));
        return { count: estimatedSlides, estimated: true };
    }

    // Spreadsheets: estimate ~2KB per page
    if (['xls', 'xlsx', 'ods'].includes(extension)) {
        const estimatedPages = Math.max(1, Math.ceil(file.size / (2 * 1024)));
        return { count: estimatedPages, estimated: true };
    }

    return { count: 1, estimated: true };
}

// Export for use in other modules
if (typeof window !== 'undefined') {
    window.CostEstimator = CostEstimator;
    window.parsePageRange = parsePageRange;
    window.detectPageCount = detectPageCount;
}
