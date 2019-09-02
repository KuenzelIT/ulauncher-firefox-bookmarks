from ulauncher.api.client.Extension import Extension
from ulauncher.api.client.EventListener import EventListener
from ulauncher.api.shared.event import KeywordQueryEvent, SystemExitEvent,PreferencesUpdateEvent, PreferencesEvent
from ulauncher.api.shared.item.ExtensionResultItem import ExtensionResultItem
from ulauncher.api.shared.action.RenderResultListAction import RenderResultListAction
from ulauncher.api.shared.action.OpenUrlAction import OpenUrlAction
from history import FirefoxHistory

class FirefoxHistoryExtension(Extension):
    def __init__(self):
        super(FirefoxHistoryExtension, self).__init__()
        #   Firefox History Getter
        self.fh = FirefoxHistory()
        #   Ulauncher Events
        self.subscribe(KeywordQueryEvent,KeywordQueryEventListener())
        self.subscribe(SystemExitEvent,SystemExitEventListener())
        self.subscribe(PreferencesEvent,PreferencesEventListener())
        self.subscribe(PreferencesUpdateEvent,PreferencesUpdateEventListener())

class PreferencesEventListener(EventListener):
    def on_event(self,event,extension):
        #   Aggregate Results
        extension.fh.aggregate = event.preferences['aggregate']
        #   Results Order
        extension.fh.order = event.preferences['order']
        #   Results Number
        try:
            n = int(event.preferences['limit'])
        except:
            n = 10
        extension.fh.limit = n
        
class PreferencesUpdateEventListener(EventListener):
    def on_event(self,event,extension):
        #   Results Order
        if event.id == 'order':
            extension.fh.order = event.new_value
        #   Results Number
        elif event.id == 'limit':
            try:
                n = int(event.new_value)
                extension.fh.limit = n
            except:
                pass
        elif event.id == 'aggregate':
            extension.fh.aggregate = event.new_value

class SystemExitEventListener(EventListener):
    def on_event(self,event,extension):
        extension.fh.close()

class KeywordQueryEventListener(EventListener):
    def on_event(self, event, extension):
        query  = event.get_argument()
        #   Blank Query
        if query == None:
            query = ''
        items = []
        #   Search into Firefox History
        results = extension.fh.search(query)
        for link in results:
            #   Encode 
            hostname = link[0]
            #   Split Domain Levels
            dm = hostname.split('.')
            #   Remove WWW
            if dm[0]=='www':
                i = 1
            else:
                i = 0
            #   Join remaining domains and capitalize
            name = ' '.join(dm[i:len(dm)-1]).title()
            #   TODO: favicon of the website
            if extension.fh.aggregate == "true":
                items.append(ExtensionResultItem(icon='images/icon.png',
                                                name=name,
                                                on_enter=OpenUrlAction('https://'+hostname)))
            else:
                if link[1] == None:
                    title = hostname
                else:
                    title = link[1]
                items.append(ExtensionResultItem(icon='images/icon.png',
                                                name=title,
                                                description=hostname,
                                                on_enter=OpenUrlAction(hostname)))

        return RenderResultListAction(items)

if __name__ == '__main__':
    FirefoxHistoryExtension().run()
