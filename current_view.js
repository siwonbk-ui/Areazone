(function(global){
  'use strict';
  function textNode(tag, text, parent){
    var el=document.createElement(tag); el.textContent=text; parent.appendChild(el); return el;
  }
  function date(value){ return value ? new Date(value).toLocaleString('th-TH', {timeZone:'Asia/Bangkok'}) : 'ยังไม่มีการตรวจสำเร็จ'; }
  function link(parent, label, url){
    if(!/^https:\/\/[^\s]+$/.test(url || '')) return;
    var a=textNode('a',label,parent); a.href=url; a.target='_blank'; a.rel='noopener noreferrer';
  }
  global.renderCurrent=function(container,data,filters){
    container.replaceChildren();
    if(!data){ textNode('p','โหลดข้อมูลล่าสุดไม่ได้ — ค่าพื้นฐาน NAT CAT ยังคงอยู่ในอีกโหมด',container); return; }
    var now=Date.now();
    textNode('p','ตรวจระบบล่าสุด: '+date(data.checkedAt)+' (เวลาไทย)',container);
    var health=document.createElement('div'); health.className='grid'; container.appendChild(health);
    (data.sources || []).forEach(function(s){
      var card=document.createElement('article'); card.className='p-card'; card.style.padding='16px'; health.appendChild(card);
      textNode('h3',s.name,card);
      var oldCheck=!s.lastSuccessAt || now-Date.parse(s.lastSuccessAt)>Math.max(4,s.intervalHours*2)*3600000;
      var oldPublication=!s.newestPublishedAt || now-Date.parse(s.newestPublishedAt)>s.staleHours*3600000;
      var status=s.status==='unavailable' ? 'ติดต่อแหล่งข้อมูลไม่ได้ — แสดงข้อมูลสำเร็จครั้งก่อน' :
        oldCheck ? 'การตรวจข้อมูลล่าช้า' : oldPublication ? 'รายงานล่าสุดเก่า — ไม่ยืนยันสถานการณ์ปัจจุบัน' : 'ดึงรายงานสำเร็จ';
      textNode('p',status,card);
      textNode('p','ดึงสำเร็จ: '+date(s.lastSuccessAt),card);
      textNode('p','รายงานใหม่สุด: '+date(s.newestPublishedAt),card);
      link(card,'เปิดแหล่งข้อมูล',s.home);
    });
    textNode('h2','รายงานและประกาศที่เผยแพร่',container);
    textNode('p','พื้นที่ที่กล่าวถึงในรายงานไม่ใช่ขอบเขตผลกระทบ · รายงานที่ยังระบุพื้นที่ไม่ได้จะแสดงไว้ให้ตรวจต้นฉบับ',container);
    var events=(data.events || []).filter(function(e){
      var age=now-Date.parse(e.publishedAt);
      if(!Number.isFinite(age) || age< -600000 || age>7*86400000) return false;
      if(filters.hazard!=='all' && e.hazards.indexOf(filters.hazard)<0) return false;
      if(filters.query && (e.title+' '+e.mentionedProvinces.join(' ')).toLowerCase().indexOf(filters.query.toLowerCase())<0) return false;
      if(filters.region && e.mentionedProvinces.length && !e.mentionedProvinces.some(function(p){return filters.regionMap[p]===filters.region;})) return false;
      return true;
    });
    if(!events.length) textNode('p','ไม่พบรายงานที่ตรงตัวกรองในแหล่งที่เชื่อมอยู่ — ไม่ได้หมายความว่าไม่มีภัยหรือพื้นที่ปลอดภัย',container);
    events.forEach(function(e){
      var card=document.createElement('article'); card.className='p-card'; card.style.cssText='padding:16px;margin:12px 0'; container.appendChild(card);
      textNode('h3',e.title,card);
      textNode('p','เผยแพร่: '+date(e.publishedAt)+' · '+(e.mentionedProvinces.length ? 'จังหวัดที่กล่าวถึง: '+e.mentionedProvinces.join(', ') : 'ยังไม่ยืนยันพื้นที่ในระบบ'),card);
      textNode('p',e.note,card); link(card,'อ่านต้นฉบับจากหน่วยงาน',e.url);
    });
    textNode('h2','แหล่งประกอบที่ยังไม่ได้เชื่อมอัตโนมัติ',container);
    (data.directory || []).forEach(function(s){var row=textNode('p','',container); link(row,s.name,s.url); textNode('span',' — '+s.note,row);});
  };
})(window);
