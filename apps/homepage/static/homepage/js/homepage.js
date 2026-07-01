/**
 * Dynamic CMS Rendering Engine
 * Translates the Django JSON payload into DOM elements based on module types.
 */
document.addEventListener('DOMContentLoaded', () => {

    // 1. DYNAMIC DATA RETRIEVAL
    // Parses the secure JSON script tag injected by Django's views.py
    const payloadElement = document.getElementById("homepage-payload-data");
    if (!payloadElement) {
        console.warn("CMS Payload element not found. Aborting homepage render.");
        return;
    }

    let cmsPayload = {};
    try {
        cmsPayload = JSON.parse(payloadElement.textContent);
    } catch (error) {
        console.error("Failed to parse CMS Payload JSON:", error);
        return;
    }

    // 2. ICON DICTIONARY (SVG)
    const icons = {
        'fair-trade': `<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>`,
        'shipping': `<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 18H3c-.6 0-1-.4-1-1V7c0-.6.4-1 1-1h10c.6 0 1 .4 1 1v11"/><path d="M14 9h4l4 4v5h-3"/><circle cx="8" cy="18" r="2"/><circle cx="18" cy="18" r="2"/></svg>`,
        'artisan': `<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>`,
        'instagram': `<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2" width="20" height="20" rx="5" ry="5"/><path d="M16 11.37A4 4 0 1 1 12.63 8 4 4 0 0 1 16 11.37z"/><line x1="17.5" y1="6.5" x2="17.51" y2="6.5"/></svg>`
    };

    // 3. COMPONENT FACTORY
    // Translates JSON data into HTML Nodes. Uses optional chaining and fallbacks for non-mandatory fields.
    const ComponentFactory = {
        dynamic_hero: (data) => `
            <section class="module-hero" style="${data.background_media ? `background-image: url('${data.background_media}');` : ''}">
                <div class="hero-content">
                    ${data.subtitle ? `<div class="hero-subtitle">${data.subtitle}</div>` : ''}
                    ${data.title ? `<h1 class="hero-title">${data.title}</h1>` : ''}
                    ${data.ctas && data.ctas.length ? `
                        <div class="hero-ctas">
                            ${data.ctas.map(cta => `<a href="${cta.url || '#'}" class="btn ${cta.style || 'btn-primary'}">${cta.label || ''}</a>`).join('')}
                        </div>
                    ` : ''}
                </div>
            </section>`,
        
        trust_bar: (data) => `
            <div class="module-trustbar">
                <div class="trustbar-grid">
                    ${(data.signals || []).map(sig => `
                        <div class="trust-item">
                            ${icons[sig.icon] || ''} <span>${sig.text || ''}</span>
                        </div>
                    `).join('')}
                </div>
            </div>`,

        visual_discovery: (data) => `
            <section class="module-discovery">
                ${data.heading ? `<h2 class="section-title">${data.heading}</h2>` : ''}
                <div class="discovery-grid">
                    ${(data.categories || []).map(cat => `
                        <a href="${cat.link || '#'}" class="category-card">
                            ${cat.image ? `<img src="${cat.image}" alt="${cat.name || 'Category'}">` : ''}
                            <div class="category-overlay">
                                ${cat.name ? `<h3>${cat.name}</h3>` : ''}
                            </div>
                        </a>
                    `).join('')}
                </div>
            </section>`,

        merchandising_carousel: (data) => `
            <section class="module-merch">
                ${data.heading ? `<h2 class="section-title">${data.heading}</h2>` : ''}
                <div class="merch-carousel">
                    ${(data.items || []).map(item => `
                        <div class="product-card">
                            <div class="product-img-wrapper">
                                ${item.badge ? `<div class="badge">${item.badge}</div>` : ''}
                                ${item.image ? `<img src="${item.image}" alt="${item.title || 'Product'}" style="width:100%; height:100%; object-fit:cover;">` : ''}
                            </div>
                            <div class="product-info">
                                ${item.title ? `<h4 class="product-title">${item.title}</h4>` : ''}
                                ${item.price ? `<div class="product-price">${item.price}</div>` : ''}
                            </div>
                        </div>
                    `).join('')}
                </div>
            </section>`,

        story_split: (data) => `
            <section class="module-maker">
                <div class="maker-split">
                    ${data.image ? `<img src="${data.image}" alt="${data.artisan_name || 'Artisan'}" class="maker-image">` : ''}
                    <div class="maker-content">
                        ${data.quote ? `<h3 class="maker-quote">${data.quote}</h3>` : ''}
                        ${data.bio ? `<p class="maker-bio">${data.bio}</p>` : ''}
                        ${data.target_url && data.button_text ? `
                            <div>
                                <a href="${data.target_url}" class="btn btn-primary">${data.button_text}</a>
                            </div>
                        ` : ''}
                    </div>
                </div>
            </section>`,

        social_proof: (data) => `
            <section class="module-ugc">
                ${data.heading ? `<h2 class="section-title">${data.heading}</h2>` : ''}
                <div class="ugc-grid">
                    ${(data.images || []).map(img => `
                        <div class="ugc-item">
                            <img src="${img}" alt="User generated content">
                            <div class="ugc-icon">${icons['instagram'] || ''}</div>
                        </div>
                    `).join('')}
                </div>
            </section>`
    };

    // 4. RENDERING EXECUTION
    // The loop that builds the page dynamically based on the Django CMS array order
    const root = document.getElementById('homepage-root') || document.getElementById('app-root');
    
    if(!root) {
        console.warn("Mounting node '#homepage-root' missing in HTML layout.");
        return;
    }

    let compiledHTML = '';
    
    const modules = cmsPayload.modules || [];
    
    modules.forEach(module => {
        if(ComponentFactory[module.type]) {
            compiledHTML += ComponentFactory[module.type](module.parameters || {});
        } else {
            console.warn(`Module type '${module.type}' missing from ComponentFactory.`);
        }
    });

    // Inject compiled DOM structure
    root.innerHTML = compiledHTML;
});