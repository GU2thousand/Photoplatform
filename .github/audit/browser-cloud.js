async (page) => {
  page.setDefaultTimeout(15000);
  const results = [], errors = [], failedRequests = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('response', r => { if (r.status() >= 500) failedRequests.push({url:r.url(),status:r.status()}); });
  const assert = (v, detail) => { if (!v) throw new Error(JSON.stringify(detail)); };
  const check = async (name, fn) => { try { results.push({name,passed:true,detail:await fn()}); } catch(e) { results.push({name,passed:false,detail:e.message}); } };
  const title = 'Cloud browser sunset ' + Date.now();
  await page.setViewportSize({width:1440,height:1000});
  await page.reload();
  console.log('INITIAL_SNAPSHOT\n' + await page.locator('body').ariaSnapshot());
  await check('registration and authenticated navigation', async () => {
    const form = page.locator('form').filter({has:page.getByRole('button',{name:'Create Account',exact:true})});
    await form.getByLabel('Name',{exact:true}).fill('Cloud Runtime');
    await form.getByLabel('Email',{exact:true}).fill('browser-'+Date.now()+'@test.example');
    await form.getByLabel('Password',{exact:true}).fill('cloud-runtime-test-123');
    await form.getByRole('button',{name:'Create Account',exact:true}).click();
    await page.getByRole('button',{name:'My Space',exact:true}).waitFor();
    await page.getByRole('button',{name:'My Space',exact:true}).click();
  });
  await check('browser direct upload, worker processing, and decoded thumbnail', async () => {
    await page.getByLabel('Title',{exact:true}).fill(title);
    await page.getByLabel('Description',{exact:true}).fill('Orange sunset cloud runtime fixture');
    await page.getByLabel('Image File',{exact:true}).setInputFiles('output/playwright/fixture.png');
    await page.getByRole('button',{name:'Upload Image',exact:true}).click();
    const row=page.locator('.library-row').filter({has:page.getByRole('heading',{name:title,exact:true})});
    await row.getByRole('img',{name:title,exact:true}).waitFor({timeout:120000});
    await row.getByRole('img',{name:title,exact:true}).evaluate(async img=>{await img.decode(); if(!img.naturalWidth) throw new Error('Empty thumbnail');});
    await page.screenshot({path:'output/playwright/photo-desktop.png',fullPage:true});
  });
  await check('private original is delivered and decoded', async () => {
    const row=page.locator('.library-row').filter({has:page.getByRole('heading',{name:title,exact:true})});
    await row.getByRole('button',{name:'Open original',exact:true}).click();
    const dialog=page.getByRole('dialog'); await dialog.getByRole('img').waitFor();
    await dialog.getByRole('img').evaluate(async img=>{await img.decode();});
    await dialog.getByRole('button',{name:'Close',exact:true}).click();
  });
  await check('keyword search shows only own matching images', async () => {
    await page.locator('.media-search summary').click();
    await page.getByRole('textbox',{name:'Image search',exact:true}).fill(title);
    await page.getByRole('combobox',{name:'Search scope',exact:true}).selectOption('mine');
    await page.getByRole('button',{name:'Find images',exact:true}).click();
    await page.locator('.media-search').getByRole('heading',{name:title,exact:true}).waitFor();
  });
  await check('team creation and real WebSocket note', async () => {
    await page.getByRole('button',{name:'Team Space',exact:true}).click();
    await page.getByLabel('Create team',{exact:true}).fill('Cloud browser team');
    await page.getByRole('button',{name:'Create Team',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.socket-status')?.textContent.trim()==='Connected');
    const note='cloud-runtime-note-'+Date.now();
    await page.getByRole('textbox',{name:'Collaboration note'}).fill(note);
    await page.getByRole('button',{name:'Send',exact:true}).click();
    await page.locator('.feed-stream').getByText(note,{exact:true}).waitFor();
  });
  await check('refresh retains authenticated session and team access', async () => {
    await page.reload(); await page.getByRole('button',{name:'Team Space',exact:true}).waitFor();
    await page.getByRole('button',{name:'Team Space',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.socket-status')?.textContent.trim()==='Connected');
  });
  await check('mobile layout has no page overflow', async () => {
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'output/playwright/photo-mobile.png',fullPage:true});
    const sizes=await page.evaluate(()=>({scroll:document.documentElement.scrollWidth,inner:innerWidth}));
    assert(sizes.scroll<=sizes.inner+1,sizes);return sizes;
  });
  await check('delete removes uploaded asset from library', async () => {
    await page.getByRole('button',{name:'My Space',exact:true}).click();
    const row=page.locator('.library-row').filter({has:page.getByRole('heading',{name:title,exact:true})});
    await row.getByRole('button',{name:'Delete',exact:true}).click();
    await row.waitFor({state:'detached'});
  });
  await check('sign-out clears private views', async () => {
    await page.getByRole('button',{name:'Sign Out',exact:true}).click();
    await page.getByRole('button',{name:'Sign In',exact:true}).waitFor();
    assert(await page.locator('.library-row').count()===0,'Private rows remain');
  });
  await check('no uncaught browser errors or HTTP 5xx', async()=>{assert(!errors.length&&!failedRequests.length,{errors,failedRequests});});
  console.log('FINAL_SNAPSHOT\n'+await page.locator('body').ariaSnapshot());
  return results;
}
