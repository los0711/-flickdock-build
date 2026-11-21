import {DOWNLOAD_HOST,installerDownload,installerName} from './downloads.mjs';
export default {
  fetch(request){
    const url=new URL(request.url),name=installerName(url.pathname);
    if(url.hostname!==DOWNLOAD_HOST||!name)return new Response('Not found',{status:404});
    return installerDownload(request,name);
  },
};
